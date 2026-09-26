"""Native TTS path preparation without importing inference or audio packages."""

import gc
import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from agent import model_assets


@pytest.fixture
def data_root():
    with TemporaryDirectory(prefix="prism-test-") as directory:
        yield Path(directory).resolve()


def source_at_bytes(root, length, unicode=False):
    remaining = length - len(str(root).encode("utf-8")) - 1
    assert remaining > 0
    name = "é" * (remaining // 2) + "x" * (remaining % 2) if unicode else "x" * remaining
    path = root / name
    path.mkdir()
    (path / "phontab").write_bytes(b"\x00\x01native-data\xff")
    (path / "voices").mkdir()
    (path / "voices" / "en").write_bytes(b"voice fixture\n")
    return path


def test_short_data_path_keeps_original_without_copy(data_root, monkeypatch):
    source = source_at_bytes(data_root, 159)
    monkeypatch.setattr(model_assets, "TemporaryDirectory", lambda **_: pytest.fail("unnecessary copy"))
    path, owner = model_assets.prepare_espeak_data(source)
    assert path == str(source)
    assert owner is None


@pytest.mark.parametrize("unicode", [False, True])
def test_long_native_path_uses_real_copy_and_preserves_payload(data_root, unicode):
    source = source_at_bytes(data_root, 160, unicode=unicode)
    if unicode:
        assert len(str(source)) < 160
    path, owner = model_assets.prepare_espeak_data(source)
    try:
        destination = Path(path)
        assert len(str(destination.resolve()).encode("utf-8")) < 160
        assert not destination.is_symlink()
        assert destination != source
        assert (destination / "phontab").read_bytes() == (source / "phontab").read_bytes()
        assert (destination / "voices" / "en").read_bytes() == b"voice fixture\n"
    finally:
        owner.cleanup()
    assert not Path(path).exists()
    assert (source / "phontab").exists()


def test_short_symlink_to_long_data_path_still_requires_copy(data_root):
    source = source_at_bytes(data_root, 162)
    alias = data_root / "alias"
    alias.symlink_to(source, target_is_directory=True)
    path, owner = model_assets.prepare_espeak_data(alias)
    try:
        assert owner is not None
        assert Path(path).resolve() != source
        assert (Path(path) / "phontab").read_bytes() == (source / "phontab").read_bytes()
    finally:
        if owner is not None:
            owner.cleanup()


@pytest.mark.parametrize("failure", ["long_temporary_path", "copy_error"])
def test_preparation_failure_cleans_temporary_copy(data_root, monkeypatch, failure):
    source = source_at_bytes(data_root, 160)
    created = []

    def temporary(**kwargs):
        owner = TemporaryDirectory(dir=source if failure == "long_temporary_path" else None, **kwargs)
        created.append(Path(owner.name))
        return owner

    def broken_copy(src, dst):
        if failure == "long_temporary_path":
            pytest.fail("must reject unsafe destination before copying")
        Path(dst).mkdir()
        (Path(dst) / "partial").write_bytes(b"partial copy")
        raise OSError("copy failed")

    monkeypatch.setattr(model_assets, "TemporaryDirectory", temporary)
    monkeypatch.setattr(model_assets.shutil, "copytree", broken_copy)
    expected = RuntimeError if failure == "long_temporary_path" else OSError
    with pytest.raises(expected, match="too long|copy failed"):
        model_assets.prepare_espeak_data(source)
    assert len(created) == 1 and not created[0].exists()
    assert (source / "phontab").exists()


def load_tts_without_audio_packages(monkeypatch):
    class BaseTTS:
        def __init__(self, **kwargs):
            pass

    tts = SimpleNamespace(TTS=BaseTTS, ChunkedStream=object, TTSCapabilities=SimpleNamespace)
    agents = SimpleNamespace(tts=tts)
    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "livekit", SimpleNamespace(agents=agents))
    monkeypatch.setitem(sys.modules, "livekit.agents", agents)
    monkeypatch.setitem(sys.modules, "livekit.agents.types", SimpleNamespace(
        DEFAULT_API_CONNECT_OPTIONS=None, APIConnectOptions=object))
    path = Path(model_assets.__file__).parent / "pipeline" / "local_tts.py"
    spec = importlib.util.spec_from_file_location("isolated_tts_paths_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "verify_kokoro_assets", lambda _: {})
    return module


@pytest.mark.parametrize("load_fails", [False, True])
def test_copy_lifetime_matches_engine_and_failed_initialization_cleans_up(data_root, monkeypatch, load_fails):
    source = source_at_bytes(data_root, 162)
    configured = []

    class EspeakConfig:
        def __init__(self, data_path=None, lib_path=None):
            self.data_path, self.lib_path = data_path, lib_path

    class Kokoro:
        def __init__(self, model, voices, *, espeak_config):
            configured.append(espeak_config)
            assert (Path(espeak_config.data_path) / "phontab").exists()
            if load_fails:
                raise RuntimeError("ONNX load failed")

    monkeypatch.setitem(sys.modules, "kokoro_onnx", SimpleNamespace(Kokoro=Kokoro, EspeakConfig=EspeakConfig))
    monkeypatch.setitem(sys.modules, "espeakng_loader", SimpleNamespace(get_data_path=lambda: str(source)))
    module = load_tts_without_audio_packages(monkeypatch)
    tts = module.KokoroTTS()
    if load_fails:
        with pytest.raises(RuntimeError, match="ONNX load failed"):
            tts.load()
        assert tts._engine is None
        assert not Path(configured[0].data_path).exists()
    else:
        engine = tts.load()
        assert tts.load() is engine
        assert len(configured) == 1
        copy = Path(configured[0].data_path)
        del tts
        gc.collect()
        assert copy.exists()  # callers can retain load()'s returned engine
        del engine
        gc.collect()
        assert not copy.exists()
    assert configured[0].lib_path is None

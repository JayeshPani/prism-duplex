"""Snapshot selection and provenance boundaries; no downloads or inference."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from agent import config, model_assets
from scripts import capture_run

SHA = "a" * 40
OTHER_SHA = "b" * 40


@pytest.fixture(autouse=True)
def clean_overrides(monkeypatch):
    for name in ("PRISM_LLM_MODEL", "PRISM_LLM_REVISION", "PRISM_ALLOW_UNFROZEN_MODELS",
                 "PRISM_MODEL_RECEIPTS_DIR"):
        monkeypatch.delenv(name, raising=False)


def test_download_selects_exact_snapshot_and_forwards_offline_flag(tmp_path, monkeypatch):
    seen = []
    path = tmp_path / "snapshots" / SHA
    def download(**kwargs):
        seen.append(kwargs)
        return str(path)
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=download))
    assert model_assets.resolve_snapshot("test/model", SHA, local_files_only=True) == path
    assert seen == [{"repo_id": "test/model", "revision": SHA, "local_files_only": True}]


@pytest.mark.parametrize("revision", [None, "main", "v1.0", "abc123"])
def test_missing_or_mutable_revision_rejected_before_download(revision, monkeypatch):
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(
        snapshot_download=lambda **_: pytest.fail("must fail before network access")))
    with pytest.raises(ValueError, match="revision"):
        model_assets.resolve_snapshot("test/model", revision)


def test_wrong_resolved_snapshot_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(
        snapshot_download=lambda **_: str(tmp_path / OTHER_SHA)))
    with pytest.raises(ValueError, match="does not match"):
        model_assets.resolve_snapshot("test/model", SHA)


def test_model_override_cannot_inherit_default_revision(monkeypatch):
    monkeypatch.setenv("PRISM_LLM_MODEL", "other/model")
    with pytest.raises(ValueError, match="revision required"):
        config.load_config("local-mac")
    monkeypatch.setenv("PRISM_LLM_REVISION", SHA)
    assert config.load_config("local-mac")["llm"]["revision"] == SHA


def test_unfrozen_override_is_explicit_and_blocked_for_benchmark(monkeypatch):
    monkeypatch.setenv("PRISM_LLM_MODEL", "other/model")
    monkeypatch.setenv("PRISM_ALLOW_UNFROZEN_MODELS", "1")
    cfg = config.load_config("local-mac")
    assert cfg["llm"]["revision"] is None and cfg["llm"]["allow_unfrozen"]
    with pytest.raises(ValueError, match="unfrozen development"):
        capture_run.validate_frozen_models(cfg)
    record = model_assets.snapshot_record("other/model", None, Path("/snapshots") / SHA)
    assert record["reproducibility"] == "unfrozen_development"


def test_mac_requests_server_snapshot_alias_instead_of_reloading_repo(monkeypatch):
    monkeypatch.setattr(config, "LLMClient", lambda cfg: cfg)
    cfg = config.load_config("local-mac")
    llm = config.make_llm(cfg)
    assert cfg["llm"]["model"] == "mlx-community/Qwen3-8B-4bit"
    assert llm.model == "default_model"
    assert not hasattr(llm, "revision")


def test_profiles_are_pinned_and_cuda_keeps_served_repo_id():
    for name in ("local-mac", "local-cuda"):
        capture_run.validate_frozen_models(config.load_config(name))
    assert config.load_config("local-cuda")["llm"]["model"] == "Qwen/Qwen3-14B-AWQ"


def test_cached_main_does_not_override_declared_revision(tmp_path):
    repo = tmp_path / "models--test--model"
    (repo / "refs").mkdir(parents=True)
    (repo / "refs/main").write_text(OTHER_SHA)
    for revision in (SHA, OTHER_SHA):
        snapshot = repo / "snapshots" / revision
        snapshot.mkdir(parents=True)
        (snapshot / "model.safetensors").write_bytes(b"fixture")
    record = capture_run.cached_model("test/model", tmp_path, revision=SHA)
    assert record["cached_revision"] == SHA
    assert record["runtime_loaded_revision"] is None


def test_loader_receipt_is_separate_from_snapshot_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_MODEL_RECEIPTS_DIR", str(tmp_path))
    path = Path("/snapshots") / SHA
    assert model_assets.snapshot_record("test/model", SHA, path)["runtime_loaded_revision"] is None
    record = model_assets.record_loaded_model("agent_stt", "test/model", SHA, path, "test")
    assert record["runtime_loaded_revision"] == SHA and not record["inference_observed"]
    saved = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert saved == record


def test_kokoro_observed_hash_is_not_claimed_as_publisher_pin(tmp_path):
    (tmp_path / "kokoro-v1.0.onnx").write_bytes(b"fixture")
    records = model_assets.kokoro_records(tmp_path)
    assert len(records["kokoro-v1.0.onnx"]["observed_sha256"]) == 64
    assert records["kokoro-v1.0.onnx"]["expected_sha256"] is None
    assert records["kokoro-v1.0.onnx"]["source_verified"] is False
    assert records["voices-v1.0.bin"]["observed_sha256"] is None


def test_kokoro_reference_match_is_not_publisher_verification(tmp_path, monkeypatch):
    import hashlib
    for name in model_assets.KOKORO_FILES:
        (tmp_path / name).write_bytes(b"test release")
    monkeypatch.setattr(model_assets, "KOKORO_REFERENCE_SHA256", {
        name: hashlib.sha256(b"test release").hexdigest() for name in model_assets.KOKORO_FILES})
    records = model_assets.verify_kokoro_assets(tmp_path)
    assert all(record["matches_reference"] for record in records.values())
    assert all(record["source_verified"] is False for record in records.values())


def test_kokoro_corruption_is_rejected(tmp_path, monkeypatch):
    import hashlib
    for name in model_assets.KOKORO_FILES:
        (tmp_path / name).write_bytes(b"corrupted")
    monkeypatch.setattr(model_assets, "KOKORO_REFERENCE_SHA256", {
        name: hashlib.sha256(b"test release").hexdigest() for name in model_assets.KOKORO_FILES})
    with pytest.raises(ValueError, match="Kokoro checksum mismatch.*kokoro-v1.0.onnx"):
        model_assets.verify_kokoro_assets(tmp_path)


def test_kokoro_missing_asset_is_rejected(tmp_path):
    with pytest.raises(FileNotFoundError, match="Kokoro asset missing"):
        model_assets.verify_kokoro_assets(tmp_path)


def test_mlx_stt_loads_path_without_repository_lookup(tmp_path, monkeypatch):
    path = tmp_path / SHA
    model = object()
    seen = []
    monkeypatch.setattr(model_assets, "resolve_snapshot", lambda *a, **k: path)
    monkeypatch.setitem(sys.modules, "parakeet_mlx", SimpleNamespace(
        from_pretrained=lambda name: (seen.append(name), model)[1]))
    assert model_assets.load_stt_model("parakeet-mlx", "test/model", SHA) is model
    assert seen == [str(path)]


def test_nemo_stt_restores_checkpoint_from_declared_snapshot(tmp_path, monkeypatch):
    path = tmp_path / SHA
    path.mkdir()
    checkpoint = path / "parakeet.nemo"
    checkpoint.write_bytes(b"fixture")
    seen = []
    model = SimpleNamespace(eval=lambda: None)
    asr = SimpleNamespace(models=SimpleNamespace(ASRModel=SimpleNamespace(
        restore_from=lambda **kw: (seen.append(kw), model)[1])))
    monkeypatch.setitem(sys.modules, "nemo", SimpleNamespace(collections=SimpleNamespace(asr=asr)))
    monkeypatch.setitem(sys.modules, "nemo.collections", SimpleNamespace(asr=asr))
    monkeypatch.setitem(sys.modules, "nemo.collections.asr", asr)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setattr(model_assets, "resolve_snapshot", lambda *a, **k: path)
    assert model_assets.load_stt_model("nemo-parakeet", "test/model", SHA) is model
    assert seen == [{"restore_path": str(checkpoint)}]


def test_evaluation_mlx_uses_pinned_path_and_keeps_word_adapter(tmp_path, monkeypatch):
    from scripts import fdb_infer
    rtb, rel = SimpleNamespace(), SimpleNamespace()
    monkeypatch.setitem(sys.modules, "run_tool_benchmark", rtb)
    monkeypatch.setitem(sys.modules, "run_tool_benchmark_all_released", rel)
    calls = []
    path = tmp_path / SHA
    tokens = [SimpleNamespace(text="Hello", start=0.1, end=0.3),
              SimpleNamespace(text=" world", start=0.4, end=0.8)]
    model = SimpleNamespace(transcribe=lambda _: SimpleNamespace(
        text="Hello world", sentences=[SimpleNamespace(tokens=tokens)]))
    monkeypatch.setitem(sys.modules, "parakeet_mlx", SimpleNamespace(
        from_pretrained=lambda p: (calls.append(p), model)[1]))
    resolved = []
    monkeypatch.setattr(fdb_infer, "resolve_snapshot", lambda repo, revision: (
        resolved.append((repo, revision)), path)[1])
    fdb_infer._patch_asr_with_mlx()
    assert rel.load_asr_model() is model
    assert resolved == [("mlx-community/parakeet-tdt-0.6b-v2",
                         model_assets.PARAKEET_REVISIONS["mlx-community/parakeet-tdt-0.6b-v2"])]
    assert calls == [str(path)]
    assert rtb.run_asr(model, "fixture.wav") == {"text": "Hello world", "chunks": [
        {"text": "Hello", "timestamp": [0.1, 0.3]}, {"text": "world", "timestamp": [0.4, 0.8]}]}


def test_evaluation_nemo_changes_loader_only(tmp_path, monkeypatch):
    from scripts import fdb_infer
    decode = object()
    rtb, rel = SimpleNamespace(run_asr=decode), SimpleNamespace()
    monkeypatch.setitem(sys.modules, "run_tool_benchmark", rtb)
    monkeypatch.setitem(sys.modules, "run_tool_benchmark_all_released", rel)
    calls = []
    model = SimpleNamespace(cuda=lambda: "cuda-model")
    asr = SimpleNamespace(models=SimpleNamespace(ASRModel=SimpleNamespace(
        restore_from=lambda **kw: (calls.append(kw), model)[1])))
    monkeypatch.setitem(sys.modules, "nemo", SimpleNamespace(collections=SimpleNamespace(asr=asr)))
    monkeypatch.setitem(sys.modules, "nemo.collections", SimpleNamespace(asr=asr))
    monkeypatch.setitem(sys.modules, "nemo.collections.asr", asr)
    path = tmp_path / SHA
    resolved = []
    monkeypatch.setattr(fdb_infer, "resolve_snapshot", lambda repo, revision: (
        resolved.append((repo, revision)), path)[1])
    fdb_infer._pin_nemo_asr()
    assert rel.load_asr_model() == "cuda-model"
    assert resolved == [("nvidia/parakeet-tdt-0.6b-v2",
                         model_assets.PARAKEET_REVISIONS["nvidia/parakeet-tdt-0.6b-v2"])]
    assert calls == [{"restore_path": str(path / "parakeet-tdt-0.6b-v2.nemo")}]
    assert rtb.run_asr is decode

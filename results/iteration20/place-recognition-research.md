# Place-name recognition: bounded research for iteration 20

Reviewed 2026-09-26. Recommendation: first compare the pinned model's existing greedy and beam decoders on exactly the same retained PCM, without changing production. Treat map confirmation and planner preservation as separate interventions. No weights were loaded or hashed, no inference/downloads ran, and no production file changed during this review. This is a development proposal informed by observed failures, not held-out evidence.

## What failed, and what remains uncertain

The [iteration 19 manual evidence review](/Users/rithikmeedinti/.codex/.chatgpt-projects/g-p-6ab6993b9c5c819198e46c20f84194b9/prism-duplex/results/iteration19/speech-fact-review.json) records `Hebbal` → `Hebel` / `Hebble`, `Indiranagar` → `Endure Inagar` / `Endurinegar`, and `Ather Grid` → `other grid` on fresh Kokoro `af_heart` input. The follow-up phrase `Third wave coffee Hebble` was then reduced to a `Hebble` lookup. The latter loses information already present in the transcript: it is a planner-fidelity defect, distinct from the recognition substitutions. Safe lookup errors prevented unsupported navigation, but did not fulfill those requests.

These observations cannot isolate Kokoro pronunciation, transport/endpointing, recognizer acoustic decisions, and lexical preference. Repeated synthetic words from the same voice are not independent speakers, and identical text is not proof of identical received PCM. There was no human listening in this review.

## Installed capabilities and boundaries

The installed package is **parakeet-mlx 0.5.2**, with model `mlx-community/parakeet-tdt-0.6b-v2` pinned to `8ae155301e23d820d82aa60d24817c900e69e487`. Its config specifies 16 kHz input, a 1,024-token vocabulary and a pure TDT/RNNT target, not a hybrid CTC model. The original model is an English FastConformer/TDT recognizer; the MLX checkpoint is a conversion. [NVIDIA model card](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2), [MLX conversion card](https://huggingface.co/mlx-community/parakeet-tdt-0.6b-v2).

Production [local_stt.py:74–96](/Users/rithikmeedinti/.codex/.chatgpt-projects/g-p-6ab6993b9c5c819198e46c20f84194b9/prism-duplex/agent/pipeline/local_stt.py:74) writes 16 kHz PCM to WAV and calls `model.transcribe(path).text.strip()`. It passes no decoder options, so the installed `DecodingConfig` uses greedy decoding. A single engine executor serializes recognition; any extra decoding cost can extend other callers' waits.

The installed [decoder:77–92,133–221,286–618](/Users/rithikmeedinti/.codex/.chatgpt-projects/g-p-6ab6993b9c5c819198e46c20f84194b9/prism-duplex/.venv/lib/python3.11/site-packages/parakeet_mlx/parakeet.py:77) has greedy and TDT beam search, but no phrase list, hotword, lexical prompt or external-LM parameter in the inspected public configuration or implementation. Beam returns one selected hypothesis, not a public N-best list. Streaming `context_size` describes acoustic attention, not named-place context. The upstream README also documents TDT beam support. [Upstream implementation](https://github.com/senstella/parakeet-mlx).

Two easy mistakes matter here:

- Python `Beam()` defaults are `(beam_size=5, length_penalty=1.0, patience=1.0, duration_reward=0.7)`, while the installed [CLI:246–273](/Users/rithikmeedinti/.codex/.chatgpt-projects/g-p-6ab6993b9c5c819198e46c20f84194b9/prism-duplex/.venv/lib/python3.11/site-packages/parakeet_mlx/cli.py:246) uses `(5, 0.013, 3.5, 0.67)`. Freeze every parameter explicitly. The NeMo beam dictionary in checkpoint metadata is not the active MLX runtime configuration.
- Token confidence exists but production discards it. Greedy uses normalized entropy; beam uses a token-plus-duration probability expression. [Decoder:454–462,579–584](/Users/rithikmeedinti/.codex/.chatgpt-projects/g-p-6ab6993b9c5c819198e46c20f84194b9/prism-duplex/.venv/lib/python3.11/site-packages/parakeet_mlx/parakeet.py:454), [alignment:7–35](/Users/rithikmeedinti/.codex/.chatgpt-projects/g-p-6ab6993b9c5c819198e46c20f84194b9/prism-duplex/.venv/lib/python3.11/site-packages/parakeet_mlx/alignment.py:7). These are neither calibrated place-correctness probabilities nor interchangeable confidence scales. A threshold alone must not authorize a destination change.

## Practical alternatives

| Option | Potential benefit and effort | Principal limitation |
| --- | --- | --- |
| Existing MLX beam, unchanged weights | Small offline integration; tests whether alternatives discarded by greedy recover names. | No map bias; may remain wrong or become slower. Local latency and memory must be measured. |
| Decoder-time contextual phrase boosting | Could favor a predeclared map vocabulary without retraining weights. | Not an installed MLX flag. Requires a decoder port or a different backend, with false known-name substitutions to evaluate. |
| Grounded confirmation after an unknown name | Can offer a small candidate set and carry the original action through an explicit answer. | Improves task recovery, not ASR. Never silently change the transcript or execute a fuzzy candidate. |
| Acoustic input/model changes | Another voice, real speech, or another recognizer could separate pronunciation and recognition effects. | Changes more variables and may add model memory. Do not tune the test renderer merely until the current recognizer accepts it. |

Current NVIDIA documentation supports GPU phrase boosting for TDT in greedy or beam decoding without model retraining. It rescales decoder token scores using a phrase tree; the current nightly documentation also exposes per-stream phrases and case-insensitive phrase handling. This is a NeMo decoder feature, not evidence of availability in pinned MLX. Its GPU/CUDA performance claims do not establish Apple Silicon performance. CTC word spotting requires a suitable CTC/hybrid path, which this pure TDT checkpoint does not provide. [NeMo word boosting](https://docs.nvidia.com/nemo/speech/nightly/asr/asr_customization/word_boosting.html), [framework word-boosting documentation](https://docs.nvidia.com/nemo-framework/user-guide/latest/nemotoolkit/asr/asr_customization/word_boosting.html).

The primary [TurboBias paper](https://arxiv.org/abs/2508.07014) supports the feasibility of decoder-time phrase trees; it does not validate an MLX port or these Bengaluru names. External n-gram fusion and N-best rescoring are larger changes; the installed decoder does not expose the needed N-best output. [NeMo customization overview](https://docs.nvidia.com/nemo-framework/user-guide/latest/nemotoolkit/asr/asr_language_modeling_and_customization.html).

For confirmation, rank only actual map candidates, retain raw ASR alongside proposed interpretations, and ask the user to select/confirm rather than treating a near spelling as authorization. Candidate state must include the pending action, expire on cancellation/new intent, and require an explicit grounded answer. Preserve full recognized venue phrases in planner queries. Avoid adding aliases such as `Hebble` from these clips: that fits observed outputs and hides recognition failures.

## Small next experiment

1. Freeze all **20** retained iteration 19 recognition inputs (10 per arm), identified by their native call and exact submitted 16 kHz PCM hash. Keep the two warmups separate. Use every correct and incorrect input, including hospital, cancellation and distance controls. Retain duplicate payloads in the declared denominator and report unique-hash counts separately.
2. Compare current explicit `Greedy()` against one declared `Beam(beam_size=5, length_penalty=0.013, patience=3.5, duration_reward=0.67)` configuration, chosen from the installed CLI rather than a post-result parameter search. Keep checkpoint, dtype, frontend, PCM and call API identical. Use balanced arm order, retain first/cold observations, and record every attempt/error. This tests decoding only; no LLM or tool dispatch is needed.
3. Save raw transcripts, token alignments/confidence, exact input hashes, queue/native/total durations and process-memory observations. Predeclare canonical-place preservation, full venue/action/negation retention, and erroneous known-place insertion checks. Report case-level changes as well as totals. An improvement on old failures is insufficient if controls regress or serial-queue latency becomes unacceptable; do not claim full RTC latency from this offline run.
4. Before any production choice, repeat the selected configuration on separately declared names, near-name unknowns, negations and varied phrasing/voices; use real user speech only when available and authorized. These would still be development checks unless a genuinely untouched evaluation is established. Test map confirmation separately so task recovery cannot mask unchanged recognition errors.

The source WAVs may form a separate renderer/transport diagnostic block, but must not replace the received native PCM in the paired decoder comparison. No experiment above has been run by this research task.

## Local source receipt

Paths below are relative to `/Users/rithikmeedinti/.codex/.chatgpt-projects/g-p-6ab6993b9c5c819198e46c20f84194b9/prism-duplex`; line references above identify inspected code. SHA-256 values bind this read-only review. Checkpoint config was read; checkpoint weights were not accessed.

| Source | SHA-256 |
| --- | --- |
| `agent/pipeline/local_stt.py` | `2515f2a29a8b4e26fa3ca98d5269bd998a2977e175e340d7f7537b3ed1f4e6e4` |
| `agent/model_assets.py` | `e2b191ea7f6bfab96b08c2f491d2fadf2e96a0b45657387a14b5ac729219d5c8` |
| `.venv/lib/python3.11/site-packages/parakeet_mlx-0.5.2.dist-info/METADATA` | `345ff9d8086a0a4f3314c955d7045959603ec44ffae5e54b027584c81e4c9d95` |
| `.venv/lib/python3.11/site-packages/parakeet_mlx/parakeet.py` | `2a4208b051ed20356908848c76080ebee7df4a554be91fc338fbe6747b19fccb` |
| `.venv/lib/python3.11/site-packages/parakeet_mlx/cli.py` | `a1d5a05d770f919ac99b1e724f104d45fad450a216d725e12af7a53e4f9e69ba` |
| `.venv/lib/python3.11/site-packages/parakeet_mlx/alignment.py` | `68e1fa4ac2d0303f8aecb70d9c8cdc9f0036514cb0f89a7ac06d15f7604097b5` |
| `.venv/lib/python3.11/site-packages/parakeet_mlx/utils.py` | `62bba06c888428ac07748815e40093b25bf4c5bd5ab683b4448741c4eaab9e47` |
| `results/iteration19/speech-fact-review.json` | `9101b1e3bf8de5dcc534309ea17a6899e089375a41b3a91cd9157a4225703811` |
| `/Users/rithikmeedinti/.cache/huggingface/hub/models--mlx-community--parakeet-tdt-0.6b-v2/snapshots/8ae155301e23d820d82aa60d24817c900e69e487/config.json` | `9bd323e60afe2615c983a5d9fc3a2c0470df2a03edf90c0f861bd59509d07264` |
| `results/iteration19-baseline/local-stack-run1/stt-diagnostics/report.json` | `b03d999d188d8957fa5cdfcb7d8ee648c1b1f11ff4285371503bbe7e7d5913ac` |
| `results/iteration19-candidate/local-stack-run1/stt-diagnostics/report.json` | `17cb3fb5aa62a749260651391c8a024b191aea74377c97d76df33025a4245b8a` |

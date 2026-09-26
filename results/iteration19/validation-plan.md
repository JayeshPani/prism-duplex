# Clarification continuation: bounded paired validation

This is a prepared development protocol, not an executed result or a frozen source checkpoint. Production changes and final recorder tests must settle before `prepare.py`, the parent-owned preflight and any rendering or inference. The machine-readable declaration is [protocol.json](protocol.json).

## Hypothesis and scope

When a real tool reports a mapped ambiguous destination for a simple navigation command, a subsequent whole, uniquely named answer should retain the original navigation request. The candidate must plan a fresh matching route and activate it, allowing the existing single semantic repair; it must not silently append calls. A cancellation or distance query is a new request and must not acquire the continuation. Unknown-place originals, no-tool clarification, ambiguous/partial answers and lifecycle races are separate boundaries, supported by scripted tests rather than assumed covered by these live cases.

Use five fresh rooms per arm, baseline then candidate, in the same declared order below. Both arms retain iteration18 recognition-aware output, counters, SDK telemetry, model/configuration bytes and the two-entry LLM cache. There is no injected recognition hold. This control is **not** iteration18's transparent-output baseline.

The baseline is a complete isolated copy of the candidate `agent` package, with only the five files in [baseline.json](baseline.json) replaced by their saved pre-continuation copies. `prepare.py` creates it once after source edits settle and verifies every copied byte. The worker inserts that package before repository ROOT before importing any `agent` module, and records actual module paths/hashes before and after running. Its existing launcher's source-inventory root is pointed at the imported package; no old file on disk is edited. The recorder itself runs from the common repository source and therefore records current-root source hashes even for the baseline; audits must distinguish recorder-source inventory from loaded worker provenance.

## Cases and expected outcomes

Every followup begins after a fresh, unique, current, non-ack reply has finished, received substantive PCM, and passed the existing quiet/input-drain guards. Do not trigger from an acknowledgment, stale handle, selected payload text or a missing trusted event.

| Order / case | First input → next input | Backend outcome | Required spoken outcome |
| --- | --- | --- | --- |
| 1 `airport_hospital_replay` | “Navigate to Airport Road.” → “I mean Manipal Hospital, Old Airport Road.” | Initial zero writes; then one fresh compute/start for `P_HOSP`, 2.2km, ETA5 | Initially ask which place; final confirms hospital navigation and five minutes, with no new clarification |
| 2 `cancel_pending_navigation` | “Navigate to Airport Road.” → “Please stop navigation.” | Zero changed-state effects; zero or one no-op `cancel_navigation` allowed; no start/waypoint | Acknowledge abandoning the request, or accurately state no navigation is active; do not claim cancellation of an active route |
| 3 `hebbal_third_wave` | “Please take me to Hebbal.” → “Third Wave Coffee, Hebbal.” | Initial zero writes; then one fresh compute/start for `K_TWC_HEB`, 12.3km, ETA31 | Initially request the full place; final confirms Third Wave Coffee, Hebbal and thirty-one minutes |
| 4 `distance_query_after_ambiguity` | “Navigate to Airport Road.” → “How far is Manipal Hospital, Old Airport Road?” | Successful distance read for `P_HOSP`; zero start, cancel or waypoint attempts | Answer 2.2km from the returned route, without claiming navigation started |
| 5 `indiranagar_ather` | “Please navigate to Indiranagar.” → “I mean Ather Grid charger, Indiranagar.” | Initial zero writes; then one fresh compute/start for `C_ATHER_IND`, 1.0km, ETA2 | Initially request the full place; final confirms Ather Grid, Indiranagar and two minutes |

The positive target distances/ETAs follow the existing Home starting position and map formula; the plan does not override map results. Airport Road has hospital/Starbucks candidates; Hebbal has Tata Power/Third Wave/Indian Oil; Indiranagar has Home/Ather/Blue Tokai. Candidate-list order is not a conversational requirement. Every positive start must use that execution's successful target compute, with active route, version1, no stops, no replaced route and `already_active=false`. Do not hardcode route IDs or an exact read-call count; direct named computation and optional valid search/reuse are acceptable. Preserve all actual reads, errors, skips and attempted mutations.

Cancellation on a fresh inactive room is a no-op: a successful `cancel_navigation` result must have `cancelled=null`, inactive/null destination/route, empty stops and unchanged version0. If no final tool runs, inactive state is inferred from the fresh room plus the complete absence of mutation attempts, not represented as a directly returned snapshot. The query may compute a route without activating it; that prepared route is not the active navigation state.

There are **10 declared captures, 20 intended input publications, 8 distinct first-rendered stems, 6 expected navigation effects, 10 final reply assessments and 4 zero-effect boundary cases**. Initial clarification is assessed separately for all10. STT segment/final counts are observed, not fixed at20. Every missing capture stays in these denominators.

## Exposure and mechanism checks

Airport Road/hospital is the known iteration18 failure; stop wording is also exposed. Hebbal/Third Wave and Indiranagar/Ather are newly composed combinations using an already known map. None is held-out evidence. All eight stems are freshly rendered once with the same pinned local Kokoro voice `af_heart`, speed1.1, untrimmed mono PCM16/24kHz, and shared byte-for-byte across arms. No rerender, preparation ASR/VAD selection, pronunciation replacement or trial retry is allowed.

Positive conversational success and mechanism activation are separate. If the model asks a valid no-call clarification, there is no structured error for the new hook. Preserve that outcome as unexercised; do not force a tool call, inject an execution or relaunch to obtain a favorable path. Where the structured destination error exists, record the original request, answer, grounded continuation, original/repaired plans and every request/dispatch. Cancellation/query must have no grounded continuation even if ordinary planning uses earlier history. Scripted tests retain immediate-context consumption, cancellation-resistant waits, synchronous event sinks, unknown places, cross-room isolation and no-call boundaries.

## Minimal reuse and prerequisite

New copies of the iteration18 lifecycle, resource collection, input generation and whole-stream ASR methods remain under iteration19. Only the continuation source selection, declared cases and final-response handling change. The recorder's new opt-in `--final-response-kind non-ack` is used for cancellation/query so a truthful no-tool `response` can complete; positive activation cases retain final result completion. All five use `--followup-trigger response-finished`. Default recorder behavior stays unchanged. A response completion or capture exit0 is not task success.

`prepare.py` is model-free and must run once before preflight. It creates `baseline-package/agent` and `source-preparation.json`; a failed or existing preparation is preserved. The parent-owned preflight must include all current agent/test/method paths, all isolated baseline package paths, the five historical source files and baseline receipt, source preparation receipt, global and both arm protocols/plans, shared observers/runtime SDK sources, actual LiveKit YAML reference and ASR protocol. Required common fields are `status: passed` and `source_sha256` mapping absolute paths to hashes. Prior full-byte model references come from iteration18 post-inference verification; no mutable model alias is acceptable.

The parent also creates `received-audio-review-protocol.json` before freeze with `script_sha256` for `review_received_audio.py` and `suite_protocol_sha256` keyed by `iteration19-baseline` and `iteration19-candidate`. Each arm plan stores the exact LLM argv, prior LiveKit YAML hash, `agent_package_root`, source variant and baseline receipt/hash. The runtime compares its effective model/coordinator/turn-handling configuration with the protocol; explicit shared environment values prevent package-local dotenv lookup from changing the comparison. No environment file is copied or inspected. Because the isolated source package changes the default relative TTS directory, the worker explicitly binds both arms to the same repository `models/kokoro` assets and records the original/selected directories and frozen model references. No model files or production source are changed by that binding.

After successful preflight, run separate supervised phases from repository ROOT:

```text
.venv/bin/python results/iteration19/supervise.py inputs
.venv/bin/python results/iteration19/supervise.py baseline
.venv/bin/python results/iteration19/supervise.py candidate
.venv/bin/python results/iteration19/supervise.py asr-baseline
.venv/bin/python results/iteration19/supervise.py asr-candidate
```

Inspect each terminal receipt and owned-process absence before starting the next phase. A failed capture is retained and the suite continues only after verified room departure; uncertain departure stops further launches. No ASR starts until both timed stacks and their children are stopped. Phase outputs are exclusive; there is no retry command or overwrite path.

## Reporting

Keep complete server/raw/promoted traces, anonymous-packet rejection, all physical calls and errors, native STT stages, loaded modules, sources/models, memory, room/PID cleanup, source audio, all received PCM and once-only whole-stream ASR. Assess final spoken facts manually against actual returned outcomes; never infer speech truth from tool effects or desired text. Separate capture completeness, recognition meaning, structured-continuation exercise, backend effects, final facts, unexpected clarification and cleanup.

Report speech-end→first navigation dispatch intention and fulfilled final reply completion with all10 rows. Dispatch intention is not backend entry; received PCM/handle completion is not physical audibility. Keep acknowledgments separate and leave their PCM unmeasured when unattributed. Report matched positive and negative cases separately; single observations and fixed arm order do not establish population p95 or causal latency improvements. A failure stays in the denominator and any followup change requires a new labeled iteration, not replacement evidence.

# Reliability iteration — 2026-09-25

This report preserves the first implementation snapshot and its measurements. Later parser, clarification, task-lifecycle, and model-provenance changes are documented in [iteration2.md](iteration2.md); the results below are not relabeled as measurements of that later source.

## Baseline and reproducibility

Source baseline: `1550f91d50c73f65a9c139bca044717130f9cc13`. Benchmark gitlink: `3e799c45a045256f47d5f1c9cda90157e2d2ec9e`. Fresh clone, initially clean; original suite **22/22 passed in 3.62 s**. New regression cases exposed defects absent from that suite. Evidence was captured from the working tree on `reliability-navigation`; source hashes identify each measured snapshot.

Machine: Apple M2 Pro, 10 CPU cores, 16 GB RAM, macOS/Darwin 24.3.0 arm64. Checks used CPython 3.11.13, Node 25.9.0, pytest 9.1.1, pytest-asyncio 1.4.0, and LiveKit Agents 1.8.3. Full package/config/hardware details are in `results/reliability/environment.json`. At this first snapshot, no local Qwen, Parakeet or Kokoro weights, LiveKit credentials, or benchmark audio were present. Runtime-loaded model revisions and live inference performance were therefore unmeasured, not zero.

Tests and scripted experiments use no model inference, microphone or audio transport. Original tests were updated where their expected behavior encoded session-wide duplicate blocking, forgiving identifier selection, or omission of cancelled invocations. New tests independently assert backend effects. `sources/` in the enclosing project was not modified.

## First-iteration checks

- Python suite: **99 passed in 3.50 s** (`final-unit.txt`).
- Frontend event/map/trace tests: **6 passed** (`frontend-tests.txt`).
- Next.js production build: passed (`frontend-build.txt`).
- Python compilation, shell syntax, local inference preflight and `git diff --check`: passed.
- Installed LiveKit entrypoint imported without loading models (`livekit-import.txt`).
- Browser: loaded UI, imported final return-to-destination trace, showed airport, no error overlay or page errors; imported cancellation trace and verified zero markers/empty navigation. Screenshots: `browser-trace.png`, `browser-cancel.png`.
- Attempted adapted audio preflight: stopped at missing `.env.local` (`audio-preflight.txt`). No audio benchmark or model inference was run.

Paths above are under `results/reliability/`.

## Retained iterations

| Hypothesis | Change | Before evidence | Decision/evidence |
|---|---|---|---|
| Exact places and explicit route state prevent incorrect navigation | Exact normalized ID/name lookup before aliases, canonical stop IDs, versioned snapshots, context-aware nearby cache | Navigation cases: 2/15 passed | 15/15 passed; retain |
| An action identity must represent intention, not all future identical arguments | Operation-scoped ledger, retry argument binding, conservative unknown state, serialized writes | Initial executor regression suite: 0/20 passed, including missing new API cases | Those cases plus 11 boundary tests: 31/31 passed; retain |
| Corrections must invalidate work before the replacement plan commits | Intent version at final transcript, dispatch/result speech guards, owned cancellation/shutdown | Three coordinator regressions: 0/3 passed | Pass after change; also test gate-to-commit race, noise blip and disconnect |
| Timer expiry is insufficient evidence of complete intent | Incomplete plans clarify without dispatch | Incomplete request executed a guessed action | No guessed action in regression; latency/clarification quality remains unmeasured |
| Existing synthetic scoring overaccepts distinct values | Exact conservative keys/values, preserved identifier punctuation, bijective matching | 1/7 scoring tests passed; weekdays and ID prefixes falsely accepted | 7/7 pass; historical outputs separately rescored |
| UI needs action identity and authoritative navigation state | Execution IDs, outcome spans, versioned map projection, cancellation cleanup, trace import | Overlapping `c1` and stale map behaviors reproduced in source | Frontend behavior tests and browser checks pass |
| Local-inference claims require explicit SDK settings | Local VAD turn/interruption options, local benchmark config preflight | Pinned SDK defaults can select hosted models | Source/API inspection and config tests; live behavior still unmeasured |

The ledger deliberately remains in memory. Durable storage alone would not create exactly-once backend effects; operation identity must participate in the backend mutation transaction or be reconciled against authoritative state. Unknown identical actions are conservatively blocked for the session; no general reconciliation UI/backend exists yet.

## Controlled before/after experiment

Identical runner and seven declared scenarios against an archived baseline and the revised source: correction before commit, correction during route computation, correction during a started write, airport→office→airport, overlapping rooms, repeated same stop by ID/name, and a read lookup. Five repeats per scenario, two policies. Policies are scaled **fixed 40/40 ms** and **read/write 20/60 ms**, with 40 ms simulated route computation; they are not production timing recommendations.

| Measure (each policy, 35 runs) | Baseline | Revised |
|---|---:|---:|
| Successful scenarios | 20/35 | 35/35 |
| Intended actions completed | 45/55 | 55/55 |
| Stale dispatches | 5 | 0 |
| Timeout runs | 0 | 0 |
| Already-started writes completing after correction | 5 | 5 |

Across both policies this is **40/70 → 70/70 successful scenarios**, **90/110 → 110/110 intended actions**, **10 → 0 stale dispatches**. Repeats are deterministic timing checks, not 70 independent conversational examples. Expected effects explicitly allow a write dispatched before correction to finish. This boundary is preserved in the evidence; it is not filtered out as a “cancelled non-call.”

Revised final-transcript timing in the final retained run:

| Policy | Dispatch p50 / p95 (115 attempts) | Effect completion p50 / p95 (55 effects) |
|---|---|---|
| Fixed | 42.537 / 85.099 ms | 84.765 / 117.833 ms |
| Read/write | 63.189 / 106.372 ms | 104.899 / 137.224 ms |

These are nearest-rank percentiles on simulated events, excluding cancelled attempts only from successful-completion latency and retaining their counts/raw attempts. They are **not speech-end, recognition, acknowledgment, meaningful spoken-response, audible interruption-stop, or real driving latency**. No such acoustic metric is available. The read/write policy reduces isolated read waiting but increases write waiting; this small constructed sample does not justify changing production thresholds. Keep the existing timing values pending audio experiments.

Raw artifacts: `results/reliability/baseline/` (original source), `improved/` (first retained integration), and `final/` (final executor integration). Each report records exact source hashes, runner hash, expectations, configuration, raw trace paths and all attempts. No benchmark item was used in these scenarios.

Reproduce the comparison from repository root:

```bash
BASELINE_DIR="$(mktemp -d /tmp/prism-baseline.XXXXXX)"
git archive 1550f91d50c73f65a9c139bca044717130f9cc13 | tar -x -C "$BASELINE_DIR"
.venv/bin/python scripts/reliability_experiment.py --repo "$BASELINE_DIR" \
  --revision 1550f91d50c73f65a9c139bca044717130f9cc13 --out results/reliability/baseline-new --repeats 5
.venv/bin/python scripts/reliability_experiment.py --out results/reliability/revised-new --repeats 5
```

## Scoring and research conclusions

Primary-source contract audit and alternatives: [research.md](research.md). Probes use invented tools rather than benchmark answers; reproduced using `scripts/probe_fdb_contract.py`. Upstream pass-rate accepts silent responses, unsuccessful backend results, extra argument keys and arbitrary dynamic-reference IDs in exact mode. An extra tool invocation or wrong literal fails. This is a scope observation, not a criticism of a result the benchmark never claims to measure.

Existing synthetic model outputs were only rescored, not regenerated: `synthetic_qwen8b_v1.json` changes 24/24→17/24; v2 changes 24/24→18/24. The scorer is intentionally conservative; formatting variants may become false negatives. Preserve both scores with scorer identity and do not claim model quality declined. Data: `development-rescore.json`. Historical FDB runs remain 5/10 and 6/10, with no new full or held-out audio benchmark claim.

Current implementation is segment-based ASR plus asynchronous tools and interruptible playback, not native simultaneous speech reasoning or incremental ASR. The research log ranks incremental/stable-prefix recognition and native duplex models as experiments, not assumed improvements. The engineering work makes no research novelty claim.

## Known limits and ranked remaining work

1. **Live audio validation:** supply LiveKit credentials and local weights; record user and listener audio on an aligned clock. Run corrections during planning, dispatch, execution and playback, cough/noise/echo/backchannels, spelling/ambiguous identifiers, disconnects and 1/2/4 concurrent rooms. Measure p50/p95 acoustic speech-end→substantive audio, dispatch, effect completion, acknowledgment separately, interruption→last audible assistant sample; publish no-response/error counts. Main events explicitly mark speech requests/handles as server proxies. Browser/WebRTC echo cancellation is not an evaluated guarantee.
2. **Verify model provenance and capacity:** iteration 2 pins Qwen/Parakeet snapshot selection and loader paths. Actual local inference, memory/queueing on target hardware, and authenticated Kokoro checksums remain unverified. A loopback address or selected snapshot alone cannot prove a server's inference provenance.
3. **Backend idempotency and reconciliation:** durable operation IDs, atomic mutation+dedup, queryable status and crash/restart/lost-ack experiments before using real reservations or purchases. Current demo has no external writes. Cancellation-resistant coroutines can outlive bounded shutdown; outcomes remain unknown until an acknowledgment arrives. Python thread cancellation cannot stop a running synchronous backend function.
4. **Clarification and false-interruption quality:** iteration 2 removes fuzzy automatic place selection and tests explicit clarification follow-up. Spelled-ID hints have no confidence-calibrated clarification. Do not claim unnecessary-clarification accuracy without an independent labeled audio set.
5. **Evaluation comparability:** run identical development WAVs through NeMo/CUDA and MLX to quantify decoding and word-timestamp drift; separately freeze future evaluation and track all prior exposure. Obtain the original Samsung Theme 05 brief and record its hash before claiming compliance. Baseline benchmark subsets were previously inspected and cannot be called untouched held-out data.

Exact remaining commands are in README: `PRISM_PROFILE=local-mac JUDGE=none LIMIT=10 bash scripts/run_benchmark.sh` for an adapted smoke run, then a separately frozen full run without LIMIT. CUDA/NeMo and optional GPT-4o require the appropriate host and judge credentials. They were not run here.

The frontend production build and browser trace import/map-clear checks were performed. Live room connection was not attempted without credentials. The package installer reported four existing dependency advisories; packages were not upgraded in this correctness iteration. Review/upgrade them before exposing the token endpoint publicly; this is not a completed security audit.

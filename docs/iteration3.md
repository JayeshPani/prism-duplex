# Planner fidelity and argument validation

This iteration follows the actual-model failures in [iteration 2](iteration2.md). It uses failure-informed development scenarios, not held-out benchmark items. All baseline and candidate outputs remain under `results/iteration3/`; no case, scorer or expected result was changed after inference.

## Named destinations and requested actions

The hypothesis and baseline source hashes were recorded before inference in `protocol.json`. Nine independently declared cases cover a named café, category searches with and without an explicit detour limit, product searches with and without a budget, a dependent cart action, independent requested actions and cancellations of earlier clauses. The runner separately checks the plan and observed mock outcomes. Catalog tools return stateless acknowledgments; navigation uses the existing fixed map.

The baseline passed **8/9**. With an active Whitefield route, “Add Third Wave Coffee, Hebbal as a stop” produced a generic coffee search with an invented five-minute detour limit. The subsequent write added **Blue Tokai**, the first nearby result. This demonstrates an actual wrong stop within the simulated map, rather than merely a different plan representation.

The retained prompt change explains three general rules: omit optional filters the user did not supply, preserve every requested action after corrections/cancellations, and resolve a named venue by its identity rather than replacing it with a category search. Tool defaults and grounded identifiers remain allowed. It contains no new benchmark examples, date formatting rules or item-specific entities, and does not strip arguments after generation.

The same nine cases then passed **9/9**. The named request searched for Third Wave Coffee, Hebbal and added the returned `K_TWC_HEB` ID. The other eight cases preserved the requested filters, actions and cancellations. Raw requests, outputs, attempts, source hashes and declared expectations are in `planner-fidelity-{baseline,guidance}/report.json`.

This is one serial baseline/candidate comparison on the pinned local Qwen server. Shared cache warmth, fixed order, a small selected sample and the combined guidance changes prevent a speedup or general accuracy claim. Prompt guidance does not guarantee that arbitrary future named requests are grounded correctly.

## Reject malformed values before dispatch

The previous plan checks validated graph structure and required argument presence but allowed invalid values through permissive scalar coercion. Before the fix, the new argument suite passed **8/46**: fractional quantities, invented boolean strings, containers substituted for strings and unknown argument keys could reach tools alongside valid writes.

The follow-up uses one manifest validation path for the resolver and executor. Known literal defects invalidate the entire plan before any backend starts. Valid dependency references are deferred only during preflight; resolved values are checked again before the dependent dispatch. Numeric strings, explicit boolean forms, optional nulls, omitted defaults and untyped filter values retain their documented behavior.

Reference validation is not a transaction: a bad value returned by a read can prevent its dependent write after another independent write has already completed. It does not roll back that completed action. The regression tests preserve this boundary.

## Fresh routes after waypoint changes

Review found a separate cache defect: compute and start an airport route, add a café stop, then compute and start a fresh direct route to the airport. The route read reused the original stop-free route ID, while activation recognized that ID as current and retained the added stop. Its reported computed route and activated route disagreed.

Including navigation version in the route-computation cache key makes that fresh request compute a new route ID and replace the old stops. Explicitly restarting the current route ID still preserves stops. The focused suite changed from **15/16 to 16/16**, with the failure and passing run retained in `navigation-route-cache-{before,after}.txt`. This is deterministic executor/backend evidence, independent of planner inference.

Whole-plan retries can recompute a different route ID after navigation changes. The executor correctly rejects changed resolved write arguments under an existing operation/call identity; callers must preserve the original resolved arguments for a retry. Replaying those original arguments is blocked after success, while a new intentional operation remains allowed. One older integration assertion expected `blocked` for a recomputed plan; the updated test checks the precise rejection, preserved original-argument replay, dispatch counts and successful navigation after cancellation. The initial full-suite failure is retained in `final-unit.txt`; this checkpoint passes **440/440** in `final-unit-retry-contract.txt`.

## Broader model regressions

After the guidance, typed-argument and route-cache changes, the unchanged 24-scenario development suite remained **17/24** (`local-synthetic-run1/`). The invented product budget and omitted neighborhood filter were fixed. A new checking-account representation mismatch and a premature apartment search replaced those two failures. The latter treated “I'm apartment hunting” as a request to search city `apartment`, then the backend raised because bedroom count and budget were absent. The later, fully specified Denver search completed correctly. Six failures concern representation/expected argument contracts and one contains an unsupported action; these categories do not change the score.

All 36 attempts are preserved: 35 successful completions and one failed read, with no observed stale dispatch, duplicate identity or timeout. Review exposed a concrete metadata mismatch: the apartment manifest advertised only city as required, while the pinned backend also requires bedrooms and max_price. Type validation cannot compensate for incorrect tool metadata or prove semantic grounding. The follow-up corrects required fields from the callable signature, without changing user data, scorer rules or backend defaults. All 12 benchmark callable signatures were checked; this was the only omitted backend requirement. The 22 new contract tests changed from 15/22 to 22/22, including rejection before any sibling write and successful complete/defaulted calls. The final Python suite passes **462/462** (`bench-manifest-full-suite.txt`).

The final unchanged 24-scenario run scores **19/24**, with all **35 attempts completing successfully** (`local-synthetic-run2/`). The vague apartment opening now asks a question without dispatch; the later fully specified search runs once. The checking-account label also matches the expectation in this run. The remaining five failures are the existing ordinal-date/grounded-extra-argument and card-label mismatches; no failures are removed or relabeled. Only apartment required-field metadata changed between the two runs, but these are repeated development examples on a warm server, and a different response to another prompt does not establish robust causal improvement. Source hashes, all outputs and the first run's premature call remain available.

The navigation integration rerun passed **4/4** declared cases, with 20 attempts, eight effects and one expected ambiguous-place error (`local-navigation-run1/`). All 19 delivered texts were reviewed. Blue Tokai now resolves its full supplied name before addition, exercising the spoken-connective lookup; no category substitution or invented detour limit appears. Returning to the airport executes a complete route computation and activation. Raw planner narration still contains unsupported claims, but it remains suppressed and the delivered summaries describe observed results. One summary unnecessarily reads out a route ID.

Substantive-text response p50/p95 was **15.669/18.730 s** over nine response turns; acknowledgment p50/p95 was **0.703/0.889 s** over ten turns. Dispatch p50/p95 was **11.406/14.588 s** over 20 attempts, and effect p50/p95 **12.230/15.748 s** over eight effects. These warm-server, final-text timings are small descriptive samples, not acoustic latency or a controlled speed comparison. The navigation run preceded the separate apartment-metadata change, which does not participate in car sessions.

The seven existing scripted scenarios, each repeated five times under both timing policies, still pass **70/70**. They complete **110/110** intended actions with zero stale dispatches and retain all ten already-started superseded write completions (`scripted-final/`). These repetitions use scripted planners, scaled holds and simulated route timing; they add no independent examples or live-audio evidence.

## Cache experiment and local-audio follow-up

An audit of installed MLX LM 0.31.3 found that PRISM's seeded requests use the sequential serving path. That path bypasses `--prompt-cache-bytes`; the recorded 256 MiB flag was not an enforced memory bound. Historical logs already showed 0.34 GB retained cache. `cache-source-audit.json` records the source hashes and locations. The one-entry cache can also evict alternating planner and responder prefixes.

The completed comparison used three fresh server processes in fixed **A1 → B2 → A1_repeat** order, with one, two and one cache entries respectively. Only `--prompt-cache-size` changed. Each process repeated the same airport → office → airport scenario three times: **9/9 trajectories passed, 63/63 tool attempts succeeded and all 27 intended navigation effects occurred**, with no backend/runtime errors or timeouts. Across the 54 model requests, corresponding request payloads and complete outputs matched between arms. Review found no unsupported factual claims in the 27 delivered result texts against the simulated tool results. Frozen source/model checks and all raw runs remain in `results/iteration3/cache-policy-run1/`; [analysis.json](../results/iteration3/cache-policy-run1/analysis.json) records the comparisons.

Timing starts at final user-text submission and ends at the substantive text callback, including about 2.5 seconds of simulated route work. These are not speech or first-token timings. All nine turns per arm, including its first fresh-process inference, are retained:

| Arm | Cache entries | Substantive text p50 | Substantive text p95 |
| --- | ---: | ---: | ---: |
| A1 | 1 | 17.787 s | 18.700 s |
| B2 | 2 | 9.119 s | 17.685 s |
| A1_repeat | 1 | 17.798 s | 18.669 s |

The two-entry median was 48.75% lower than the mean of the two one-entry medians for this workload. First fresh-process responses remained **17.649/17.685/17.698 s**, so no initial-request benefit was observed. Server logs support the prefix-reuse explanation: all first planner requests processed 1,765 remaining prompt tokens; the next planner request processed 2,006 with one entry versus 272 with two. At the next scenario repeat, those counts were 1,761 versus 27. These counts describe tokens remaining after cache lookup, not full prompt lengths or API-reported cache-hit totals.

The nominal 256 MiB byte flag remained **unenforced** on the installed MLX LM 0.31.3 seeded sequential path. Logged logical cache grew to **0.38 GB with one entry and 0.68 GB with two**. Sampled server RSS peaks were approximately 4.9 GB in all arms, but this does not demonstrate zero memory cost: RSS is not total unified-memory use, sampling can miss peaks, system available memory varied, and roughly 4.33 GB of swap was already occupied. System-wide swap traffic cannot be attributed to cache size from these measurements; simultaneous STT/TTS memory was not measured here.

The observed return to slower timing in the final A arm, identical model outputs and reduced prefill support retaining two entries for subsequent local demo experiments with memory monitoring. This is one B process between two A processes, with repeated cases sharing each process's cache; the 27 turns are not independent trials. Fixed order, background applications, thermal state and shared OS/filesystem/Metal caches remain confounds. The result does not establish a general twofold speedup, concurrent capacity or an enforced production memory bound.

LiveKit is now installed, and ten localhost RTC capture trials exist under `results/iteration4/` using self-hosted transport; Cloud credentials were unnecessary. [LiveKit's local setup](https://docs.livekit.io/transport/self-hosting/local/) documents development credentials. The [iteration 4 review](iteration4.md) reports actual effects, received-audio transcription, response latency, correction tails and incomplete initial-state capture; capture completion alone is not an interruption-quality result. The development recorder preserves received PCM and frame-arrival timestamps, separately from the upstream benchmark recorder that allocates output by input duration and concatenates frames. Physical microphone echo, road noise and speaker latency remain unverified. The public benchmark archive landing page was reachable, but its download and fresh evaluation were not performed.

## Reproduce

With the pinned local model server running, use a new output path:

```bash
.venv/bin/python scripts/planner_fidelity_experiment.py \
  --out results/iteration3/new-fidelity-run --timeout 120
.venv/bin/python -m pytest -q
```

To repeat the cache comparison, first stop the task-owned model server and ensure port 8081 is free:

```bash
.venv/bin/python scripts/cache_policy_experiment.py \
  --launch-record results/iteration2/local-llm-server/launch.json \
  --out results/iteration3/new-cache-comparison --arm-timeout 300
```

Remaining priorities are broader live-audio interaction cases beyond the two measured RTC scenarios, named-place and action-coverage evaluation, further controlled response-latency work, and then separately labeled adapted FDB evaluation. Durable backend reconciliation, bounded telemetry archival, CUDA/MLX ASR comparability and organizer-issued Theme 05 requirements remain outside the evidence established here.

# Speak confirmed navigation results without another model call

Iteration 7 recorded 0.803–3.452 seconds between receiving navigation success and receiving its result text. Those intervals include responder work and scheduling/transport, rather than isolating model service time. The responder nevertheless made a second Qwen request to express destination and ETA fields already returned by the tool. This iteration tests constructing those replies directly.

## Retained behavior and boundaries

The responder directly formats one successful `start_navigation`, `add_waypoint` or `cancel_navigation` outcome when the complete execution contains only that mutation and its successful/reused prerequisite reads. It uses returned destination, stop, replacement, current ETA and no-op fields. It does not recover these facts from the user's text or the place catalog. Already-active navigation and already-present stops receive distinct wording; cancellation with no active route does not claim to have stopped one.

Independent reads, multiple mutations, incomplete/stale executions, blocked retries, errors and unsupported result shapes retain the existing model path. Unknown writes and place clarification keep their earlier deterministic responses. A prepared route alone never qualifies as started navigation. Templates exceeding thirty whitespace-delimited words also fall back.

An initial candidate overlooked information requested from a prerequisite: “list the coffee places and add the nearest” still needs the list. Review reproduced this and a navigation-plus-distance omission. The final candidate additionally requires a complete recognized action-only command matching planned literal queries or returned names. Unfamiliar phrasing, extra questions and language/style modifiers retain model synthesis. This is a limited optimization, not a second general intent parser. For example, a shortened hospital name in the frozen development inputs remains on the model path. The coordinator supplies repaired text when available; this gate cannot independently establish that the resolver preserved every part of the original utterance.

Only `agent/coordinator/responder.py` changes production behavior. Planning, tools, commit policies, telemetry and the session/job lifecycle are unchanged. This is an engineering reduction in redundant generation, not a new duplex-model research claim.

## Tests and isolated summary comparison

The initial twenty cases produced seven failures and thirteen passes on the model-only baseline. The first candidate passed them and 531 full-suite tests, but the two information-omission regressions failed that preserved candidate. After the request guard, the focused suite passes **123 tests** and the full suite **538 tests**, including 27 new navigation-summary cases. Coverage includes changed destination/ETA, preserved stops, actual and repeated additions, cancellation/no-op, stale historical retries, reused dependencies, mixed failures, foreign result names/numbers and fallback boundaries. Tests completed before timed inference.

Ten independent development executions were constructed with the real simulated-map tools/executor, then frozen. Their setup and action events remain in `inputs.json`; zero route delay applies only to this untimed construction. The timing experiment replays identical completed outcomes through baseline A and candidate B in A/B/B/A order on one fresh, pinned local Qwen server: **40 declared/completed summaries**, twenty per arm. No tools execute, and no STT/TTS/RTC runs during measurement. Request logging is flushed after the summary timer; all prompts, outputs, failures and logical model calls are retained.

| Original eight cases expected to use direct summaries | Baseline A | Candidate B |
| --- | ---: | ---: |
| Completed rows | 16/16 | 16/16 |
| Summary duration, nearest-rank p50 | 1967.104 ms | 0.121 ms |
| Summary duration, nearest-rank p95 | 4677.863 ms | 2910.960 ms |
| Rows without a logical model call | 0/16 | 14/16 |

The hospital-via case was originally expected to be direct. Its short request does not match the final guard's full destination label, so both B occurrences fall back. That failed coverage expectation remains in the original sixteen-row denominator; it is not removed to improve the p95. The fourteen actual direct rows take 0.049–0.460 ms, a descriptive subset. Across all ten cases, A makes twenty logical model calls and B makes six, rather than the originally expected four. Logical calls are not counts of transport retries.

All forty texts were reviewed against their frozen results. Direct replies retain current destinations, ETA values and no-op status. The baseline's two already-active replies incorrectly say the route was “started again.” Several model replies omit supplementary result context, and four candidate fallback rows still include unnecessary route IDs. Supplementary omissions are distinguished from missing explicitly requested answers; this is a qualitative development review, not a general accuracy score.

Shared-server A/B/B/A is not equal-cache control or independent sampling. Model fallbacks at block ends affect subsequent cache state, the first cold response remains included, and background applications are not isolated. These numbers establish the behavior of the measured summary stage. They do not establish the same reduction in complete conversations, whose history, planning and synthesis also change. Original/superseded sources, the unexecuted earlier protocol and test-label/collection mistakes are preserved and explained in [protocol clarifications](../results/iteration8/protocol-clarifications.json).

## Four actual audio conversations

A separate frozen suite ran baseline, correction, correction, baseline in four fresh rooms with the current code and exact earlier WAVs. All four recorder processes completed without cleanup errors. The audit verifies **18 successful attempts and six expected effects**. Both corrections interrupt the initial airport result and finish an office result. Full client/server trace equality holds for **3/4**; the remaining capture misses only initial listening. Every room logs PRISM cleanup and loses its agent participant. The worker exits 0 without escalation, 0.956497 seconds after its drain log. All observed service, recorder and review-process PIDs were absent afterward.

Across the six turns, successful-navigation receipt to result-text receipt is **1.362–2.521 ms**; receipt to first result PCM is **0.977–1.487 seconds**. These are client observations, not exact server service time or physical playback. The earlier 0.803–3.452-second text interval is historical context with a different run, not a controlled RTC A/B comparison.

| Speech-end to observation | Initial requests, n=4, p50/p95 | Corrections, n=2, p50/p95 |
| --- | ---: | ---: |
| Acknowledgment PCM | 3.062 / 3.152 s | 3.081 / 3.200 s |
| Navigation-success receipt | 7.698 / 15.949 s | 8.600 / 9.974 s |
| Substantive PCM | 8.683 / 16.998 s | 10.058 / 11.461 s |

The two intentionally interrupted initial result handles remain null for completion timing. Corrected handles finish at 16.848/18.282 seconds. Old-result non-silent PCM last arrives 738.425/739.634 ms after correction onset, including untagged tail. Tiny samples, fixed order and shared warm models limit inference; the slow first response remains included. Planning and synthesis still dominate the interaction.

After services stopped, pinned local Parakeet reviewed all four whole received streams without trimming or retries. Each transcript retains its destination/replacement and duration value (airport 88 minutes, office 29), with proper-name spelling differences. **All four transcribe “ETA” as “Ida.”** This is a clarity issue to investigate, not evidence that the acronym is intelligible. The proxy cannot distinguish TTS pronunciation from ASR recognition; no human listening was performed.

Thirteen structured worker warning entries remain: five native unknown-handle warnings, six phonemizer warnings and two watchdog intervals of 100.1/353.3 ms without sampled stacks. A separate startup key-length warning is also retained in the raw log. One native handle repeats at worker shutdown. Successful cleanup does not establish warning-free operation or explain these stalls. The 238 memory samples show available system memory down to 0.681 GB, swap 5.226 GB initially/8.309 GB finally/8.502 GB maximum, and LLM/worker peak RSS 4.828/1.441 GB. Background applications, existing swap and two process-sampling errors are retained; these are not capacity measurements.

## Reproduction and next work

Raw inputs, protocols, source/model hashes, process receipts, all forty texts, four PCM captures and whole-stream ASR are under `results/iteration8/`. [Summary analysis](../results/iteration8/summary-analysis.json) and [RTC analysis](../results/iteration8/latency-summary.json) expose every denominator and failure. All frozen sources/input bytes remained unchanged through each experiment. Model inference is local; default transport ICE/STUN may contact network services.

For a fresh summary repeat, retain this folder and use a new sibling under `results/`. The runner resolves its inputs beside itself and refuses an existing report. Its frozen source keys still reference the originals, so verify the copied inputs too. From the repository root, with the recorded local Python/MLX environment, pinned Qwen files and free port 8081:

```bash
mkdir results/iteration8-summary-repeat
cp results/iteration8/{measure_summaries.py,prepare_inputs.py,inputs.json,responder-before.py,responder-candidate.py,summary-protocol.json,protocol-clarifications.json,analyze_summaries.py} results/iteration8-summary-repeat/
.venv/bin/python - <<'PY'
import hashlib, json
from pathlib import Path
folder = Path('results/iteration8-summary-repeat')
protocol = json.loads((folder / 'summary-protocol.json').read_text())
for name, expected in protocol['frozen_sha256'].items():
    if name.startswith('results/iteration8/'):
        assert hashlib.sha256((folder / Path(name).name).read_bytes()).hexdigest() == expected, name
PY
.venv/bin/python results/iteration8-summary-repeat/measure_summaries.py
```

The copied `analyze_summaries.py` can derive numerical summaries, but its qualitative annotations are manual observations from this original run, assigned by arm and case. Read all forty fresh texts against their inputs and update those annotations in the copied script before running it; changed model replies must not inherit old judgments. Preserve the original analysis. This comparison replays completed outcomes; it is not a new tool/planner experiment.

Repeat RTC using the fresh-directory procedure in [iteration 7](iteration7.md), substituting `iteration8` for the source folder. Use the four-case protocol and copy its review method; wait for all owned services to exit before offline ASR. Keep the original evidence intact and treat changed code/inputs as a new run.

Retain the response path with its documented fallback coverage. Next, test spelling out the ETA phrase against the observed acronym issue and inspect original-versus-repaired request fidelity. Slow planning, microphone/echo behavior, short-command/empty-transcript interruption recovery, active-tool disconnects, simultaneous live rooms, fresh adapted FDB evaluation and organizer Theme 05 verification remain unfinished. There are now **34 actual RTC captures across four frozen iterations**, not thirty-four trials of this latest source.

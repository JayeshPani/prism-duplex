# Keep disk logging off the audio event loop

Iteration 5 recorded a 612.5 ms blocked-event-loop watchdog interval, with a sampled stack inside the durable tool logger's snapshot replacement. It did not establish that one replacement consumed the whole interval or caused a missed interruption. This iteration addresses the blocking path while preserving dispatch and cancellation guarantees.

## Retained change

The executor now runs the thread-safe attempt logger's disk operations through worker threads. It owns those tasks separately from backend calls. A normal call still waits for its intention to persist before entering the backend, and waits for completion logging before returning. Disk service time and snapshot growth remain; the change lets audio/turn handling continue while that I/O waits.

A correction during start logging releases the undispatched ledger reservation and prevents backend entry. The surviving disk operation remains owned; if it succeeds, the same intention is finalized as cancelled. Start failure prevents dispatch, including failure after a partial persistence. After successful persistence, intent is checked again immediately before protection and backend entry. Writes already entered remain protected as before.

Completion logging cannot change a known backend outcome. Cancellation or a cleanup timeout while saving a successful result leaves that action done. Shutdown includes detached logging work, waits for at most two configured timeout windows, and reports pending telemetry separately from uncertain effects. It never claims that cancelling an asyncio task stops its underlying disk thread. Default thread-pool/process shutdown can still wait for blocked native I/O; this is not a process-exit guarantee.

Resolved arguments are copied before waiting and isolated between the start event, logger and backend. Review also found a cache race introduced by the new persistence wait: a read could observe state B and save it under the earlier context A. The executor now refreshes read context after start persistence and only caches if that context still matches at completion. The completion guard also covers an analogous pre-existing change during an asynchronous backend read. Context providers must identify all mutable dependency versions; unversioned keys cannot detect an invisible A→B→A change.

Custom `attempt_logger` callbacks must be thread-safe. The live FDB logger retains its existing thread/process locks, atomic replacement and fsync sequence. Arbitrary legacy `tool_logger` callbacks and event sinks remain synchronous, so this is not a claim that all blocking work has left the event loop.

## Regression evidence

Twelve initial fault-injection cases produced ten failures and two passes on the synchronous baseline. The first candidate passed these and the 503-test full suite. Two additional cache cases then failed that candidate; they require correct fresh results and continued reuse in stable contexts, so disabling caching does not satisfy them. Those failures and the superseded source are retained.

The final focused suite passes **71/71**, including fourteen new cases, and the full suite passes **505/505**. Coverage includes held start/completion I/O with an independently running loop witness, durable intention visibility inside the backend, read/write cancellation before dispatch, partial persistence failure, known success during cancellation, bounded cleanup with late finalization, mutable resolved arguments, post-persistence intent changes, both cache races and truthful coordinator shutdown reporting. Existing crash-survival and unmodified upstream collector checks also pass.

## Controlled slow-I/O comparison

The protocol was declared before measurement. Each fresh subprocess executes one mock write using the real FDB snapshot logger. A 100 ms sleep is injected immediately before snapshot replacement, either during start or completion logging. An asyncio ticker wakes every 5 ms. Each table entry is the median of the maximum ticker lateness in five trials; it is not a conversational latency percentile.

| Injected delay | Synchronous baseline | Final implementation | Expected effects and finalized rows |
| --- | ---: | ---: | --- |
| Before start replacement | 99.749 ms | 0.706 ms | 5/5 in each condition |
| Before completion replacement | 104.061 ms | 0.677 ms | 5/5 in each condition |

All twenty baseline/final trials completed, with one durable intention at backend entry and one final done row per effect. Ten trials from the first candidate are also retained. The helper and protocol hashes match across all three conditions; the final executor hash matches the measured final source.

All thirty measurement subprocesses used the project environment's **Python 3.11.13**. The frozen protocol's `baseline.python` field contains **3.9.6**, the system interpreter used to write the declaration; it does not describe the measured runtime. That metadata error remains visible in the original protocol and is corrected here and in `protocol-clarifications.json`.

These results support freeing the loop during the injected disk wait. They do not show faster disk operations, improved physical interruption timing or live-room capacity. Conditions and phases ran in fixed order, background machine load was not isolated, and regression checks overlapped portions of measurement. The injected sleep reproduces a blocking interval, not the unknown storage cause of the iteration 5 watchdog warning. No STT, LLM, TTS, RTC or microphone inference ran in this comparison.

## Reproduction and evidence

From the repository root, with the existing test environment:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python results/iteration6/measure_loop.py \
  --executor-source results/iteration6/baseline-source/agent/coordinator/executor.py \
  --out results/iteration6/new-slow-io-baseline
.venv/bin/python results/iteration6/measure_loop.py \
  --executor-source agent/coordinator/executor.py \
  --out results/iteration6/new-slow-io-candidate
```

The helper refuses existing output directories and records each command, exit, complete tick series, injected sleep, source hash and tool row. It loads the selected executor source with the repository's other modules; this comparison assumes the unchanged ledger and logger versions recorded in the protocol. Run measurements separately from tests to avoid additional background load. The original run directories remain unchanged.

[The comparison](../results/iteration6/loop-comparison.json) is derived by `results/iteration6/analyze_loop.py` from all three original run reports. `results/iteration6/` also contains the protocol, baseline and superseded source snapshots, failing tests, focused/full suite logs and raw rows. The first `full-suite.txt` is the 503-test checkpoint; `full-suite-final.txt` records 505 passing tests after the cache fix.

## Remaining work

The **22 RTC captures in iterations 4–5 predate this executor change**. Their measurements remain evidence for those frozen sources, not a fresh live-audio validation of the threaded logger. Repeat the audio/correction scenarios with the new source before claiming a live latency improvement. The worker's forced shutdown remains unresolved; configure and measure its SDK drain separately. Logging still rewrites all retained history, pending disk work can outlive shutdown, and blocked threads can delay other work sharing the default thread pool. Human microphone/echo tests, explicit empty-transcript false-resume evidence, simultaneous live rooms, a fresh adapted FDB run and organizer-issued Theme 05 verification remain outstanding.

Follow-up: [iteration 7](iteration7.md) records eight RTC captures with this logger and fixes the missing session-close-to-job-shutdown connection. All expected effects and normal worker exit were observed. This paragraph updates the backlog; the measurements and source hashes above remain the iteration 6 checkpoint.

# Alex MCP reliability audit — 16 October 2026

Baseline: `main` at `d5c999b0d59f97ea622c99491a720cea82d676d6`, version 0.5.38.
Repair release: 0.5.39.

## Finding

The existing architecture is a sound foundation. The baseline passed 459 Python
tests and the external Alex Lab, including 137 behaviour contracts and 1,147
variant checks. Additional failure-path and concurrency tests nevertheless found
the defects below. These are implementation repairs within the existing system;
they do not require an additional agent framework or additional model calls.

## Confirmed defects and repairs

| Finding | Evidence on baseline | Repair |
| --- | --- | --- |
| Semantic routing's timeout did not bound the request's actual return time | Cancelling `asyncio.to_thread` left its worker running; ingress's `asyncio.run` waited for the default executor during shutdown. A simulated 30 ms deadline with a 350 ms call returned in 352 ms. | Two bounded daemon workers, no queued overflow, immediate fallback at the deadline, discarded late results. The identical probe returns in 31 ms. |
| Reaction failures could prevent unpinning | Cleanup returned immediately when hourglass removal failed. Exhausting the shared reaction retry budget also blocked a later unpin. | Unpin first; a different control operation starts its own bounded retry sequence. Failed pinning cannot have its retry budget reset by a successful cosmetic reaction. |
| Delivered documents could retain their pin indefinitely after a temporary cleanup failure | With startup reconciliation already complete, repeated outbox sweeps never attempted cleanup for a delivered document. | Each sweep selects a bounded set of unfinished, retryable delivered-document controls, honoring backoff and permanent failure diagnostics. |
| Recurring/snoozed HA reminders stopped notifying after the first occurrence | Queueing occurrence A twice and occurrence B twice returned `[1,0,0,0]`; the unique key used only reminder identity and phase. | Include the due occurrence in the deduplication key. The same test returns `[1,0,1,0]`. |
| HA could send completed reminders after an outage | A due notification queued before completion was still sent by the recovering HA outbox. | Recheck reminder existence/status before sending due/follow-up notifications; suppress completed/cancelled entries, including legacy queue keys. |
| Presence requests held SQLite's writer lock | A second connection could not start a write while the scheduler fetched presence; HA requests can each wait up to their configured 15-second timeout. | Fetch relevant presence before taking the write lock. Re-read reminder state under the transaction; a concurrent completion prevents delivery. |
| Two simultaneous redeliveries could reclaim one stale inbound lease | A synchronized test against the original function returned `CLAIMED` twice for one message. | Wrap the lease check/update in `BEGIN IMMEDIATE`; one worker claims and the other gets `DUPLICATE`. |
| An isolated rejected queue task created an unhandled internal promise rejection | Node exited under strict unhandled-rejection handling even though the caller caught the task error. Existing tests queued a second task immediately, hiding the fault. | Keep the internal scheduling tail resolved while returning the original task promise to its caller. Existing ingress wrappers already catch most task errors, so this is a latent resilience defect. |

## Audit scope and validation

Reviewed the current ingress/identity boundary, group/reply routing, private-scope
and numbered-selection handling, model/tool orchestration, mutation claims,
reminder ownership and scheduling, WhatsApp/HA outboxes, report delivery and
release checks. Domain behaviour across the 121-tool surface is exercised by the
existing regression and external-lab suites. This is not a claim of exhaustive
line-by-line proof or live household certification.

- Full repaired Python suite: **473 tests passed** (459 existing + 14 new).
- New Python cases cover deadline return, worker saturation/result/error,
  unpin failure paths, bounded pin retries, document cleanup retries,
  occurrence deduplication, stale notification suppression, writer-lock freedom,
  concurrent completion and inbound claims.
- Node queue and control/reaction/quoted-context contracts passed, including the
  isolated-rejection case with `--unhandled-rejections=strict`.
- Python compilation, JavaScript syntax and undefined-name checks passed.
- Four older tests changed only their expected cleanup sequence and the index
  of the reaction payload assertion: unpin now precedes reaction removal. Their
  ownership, completion, message-target and marker-state checks remain present.
- External Alex Lab and GitHub test/container gates are recorded on the release
  pull request. No gate or frozen corpus was weakened to make this repair pass.

## Latency and compatibility

No new AI classifier, reviewer, planner or per-message network call was added.
The optional live semantic router retains its existing six-second deadline;
the repair makes that deadline effective at event-loop shutdown. At saturation,
optional routing falls back to the established route immediately. Timed-out
network work cannot be forcibly killed, but at most two such workers exist.

SQLite claim serialization is a short local transaction. Presence IO now occurs
outside the writer lock. Document-cleanup retries add a bounded local query and
perform control IO only when unfinished cleanup is eligible. These tests verify
specific latency failure paths; they do not measure production WhatsApp/provider
p50 or p95 reply latency on the owner's Mini PC.

No household schema migration is required. Existing scope/ACL rules, finance
calculations, reminder ownership, acknowledgement/completion semantics, deferred
voice policy, tool descriptions and model defaults remain in force. The version
is aligned in add-on configuration, Docker build metadata and MCP server metadata.

## Remaining live verification

The engineering environment has no connection to the running HA add-on,
WhatsApp session, household database or current configured model credentials.
No live messages were sent and no household devices were controlled.

After installing 0.5.39 through the existing HA add-on update flow:

1. Exercise family reminder → claim → DM → due → acknowledgement → completion;
   verify the active message remains pinned until completion, then unpins.
2. Send a PDF report and verify original-file delivery and marker cleanup.
3. If HA Companion reminders are configured, snooze a due reminder and verify
   another phone notification. Complete a reminder during an HA outage and
   verify no stale due notification appears when HA recovers.
4. If live semantic routing is enabled, compare actual reply latency and inspect
   router timeout/fallback diagnostics under slow-provider conditions.

The local and CI results support releasing these repairs. They cannot establish
that every WhatsApp client renders pins correctly or that real provider language
understanding is flawless. The existing quote-bound error export is the right
evidence source for any remaining live failure.

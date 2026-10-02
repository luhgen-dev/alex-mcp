# Alex MCP v0.5.6 — Live Regression Audit Handoff

**For:** Claude, independent external auditor  
**From:** ChatGPT, chief engineer  
**Date:** 2 October 2026  
**Audit mode requested:** independent, read-only, full-source second opinion  
**Repository:** https://github.com/luhgen-dev/alex-mcp  
**Exact live-tested baseline commit:** `af28a8182113f3cf5f132bd0e00934ea13dd7fe6`  
**Installed Home Assistant version under test:** v0.5.6  
**Repair branch (do not treat as baseline):** `repair/v0.5.7-live-regression`

## 1. Purpose

v0.5.6 passed its pre-release CI/certification suite, but natural WhatsApp live testing on the exact merged baseline above exposed several important interaction and state-lifecycle defects. Please audit the **complete source at the exact baseline commit**, not only this report, and determine the root causes and any coupled defects that the live symptoms imply.

This is a second opinion. Do **not** modify the repository, branches, pull requests, Home Assistant, or production data. Use disposable/sandbox state for reproductions.

Please distinguish findings as **DEMONSTRATED**, **STRONGLY SUPPORTED**, or **SPECULATIVE**. For every finding, identify exact files/functions/paths, explain the mechanism, and recommend the smallest robust repair plus regression tests.

## 2. Release-critical product invariants

### Finance is the source of truth

Finance accuracy is release-critical. Alex is intended to be the household ledger the user can rely on months later.

Required invariants:
- Preserve the active report period/category/scope across natural follow-ups.
- Every subtotal must reconcile to the underlying transactions.
- PDF/CSV/JSON exports must represent the same dataset being discussed.
- Explicit quoted/swipe-replied messages outrank unrelated recent conversational context.
- Shared/private finance scope must be deterministic.
- If the intended period/dataset is ambiguous, ask rather than silently switch.
- Never confidently report a number from the wrong month/category merely because that data is available.

### Privacy / natural scope

Locked behavior:
- Plain DM reads default to **FAMILY_SHARED**.
- User should never need to say “shared” in normal conversation.
- Explicit word **private** or **any emoji in the current command** selects owner-private.
- In the family group, private intent must not reveal private data or even private-result details; acknowledge and continue in owner DM.
- One spouse can never access the other spouse’s private records.
- Normal examples such as “Show me my saved notes” should therefore return Family Shared only.

### Private discovery

Owner-only DM should support fuzzy/private discovery without requiring the original title:
- “Show me all my private saved notes.”
- “Do you remember me saying anything about keys?”
- Search by subject, approximate date, description, person/place, media/receipt/asset metadata, indexed content.
- For a plain/shared query with no result, Alex may offer: “I can also check your private records if you want,” without revealing that a private match exists.
- A direct “Yes” to that specific offer authorizes that private follow-up.
- Results should be numbered, use human descriptions + local date/time, and hide raw UUID/media IDs in normal UX.

### Family reminder accountability

Current claim behavior is useful and must be preserved, then extended:
- Family reminder can be claimed **before due** by any emoji reaction.
- Claimed responsibility moves to claimant; due reminder goes to claimant DM, not group.
- Due claimant reminder must be **pinned** and remain unresolved until explicit completion.
- Before due: any emoji may claim.
- After due: ordinary reactions such as 👍/❤️/😂 mean seen/acknowledged only; they must **not** complete.
- After due: ✅ or explicit completion text (“Done”, “Picked it up”, “Completed”) resolves and unpins.
- If ignored: nudge claimant first; if still unresolved, privately notify the original initiator.
- Initiator should be able to: remind claimant again, reopen to family, mark done, or cancel.
- Claimant can relinquish (“I can’t do this”) and reopen to original group.
- Preserve one logical reminder/task ID through claim, due, escalation, reopen, completion.
- Alex-managed pins must not disturb unrelated/manual WhatsApp pins.

## 3. Live PASS evidence on v0.5.6

These behaviors worked live and should be protected against regression.

1. **Leave re-add / tombstone repair — PASS**
   - Full-day annual leave on 6 Oct 2026 successfully restored and retrieved.

2. **Emoji-private receipt lookup + group handoff — PASS**
   - Family group: “show me the Watsons receipt from 1 October 😊”
   - Group only acknowledged private handling.
   - Correct receipt arrived in owner DM.

3. **Natural finance amount/detail lookup — PASS**
   - “What did I spend RM6.50 on yesterday?”
   - Correctly returned “nasi lemak for lunch.”

4. **Calendar resolution — PASS**
   - “Remind me on Saturday at 10:00 AM to wash the car.”
   - Resolved to Saturday 3 Oct 2026, not Sunday.
   - Stale v0.5.5 Sunday fixture was then cancelled.

5. **Compound September PDF generation — PASS**
   - “Send my September 2026 finance report as a PDF file and tell me my pocket cash balance.”
   - PDF attached and opened.
   - Plain DM shared report correctly excluded private cash.
   - September shared report: MYR639.78 total, 6 tx; Housing/Maintenance 593.62; Transport 30.49; Food & Drink 15.67.

6. **Private Pocket Cash data — PASS**
   - Plain query did not expose private pool.
   - Emoji-private query returned MYR100.00.

7. **Pre-due family reminder claim — PASS**
   - Family reminder claimed by reaction before due.
   - Immediate claimant DM confirmation.
   - At due time, claimant received private reminder; no intended group due duplicate.

8. **Voice safety/provenance core — PASS**
   - Voice is not trusted as command input.
   - Incoming audio saved as pending/unresolved.
   - Original audio remains retrievable.
   - Numbered list mechanism exists; bare “1” in reply context retrieved audio.
   - Accidental spoken “resolve 1” correctly became another pending voice note rather than executing a command.

9. **Private note exact-match boundary — PASS**
   - Plain “Show me v058 test” did not expose private record.
   - Emoji-private version found the private “Clothes in dryer” note.

10. **Private saved-note browse — PASS**
   - “Show me all my private saved notes.” returned owner-private notes only, numbered.

11. **Shared note browse — PASS**
   - “Show me all my shared saved notes.” returned Family Shared only.
   - More importantly, natural “Show me my saved notes” also returned Family Shared only.

12. **Direct filtered PDF export — STRONG PASS**
   - “Send me a PDF of my September transport expenses.”
   - Actual PDF attached.
   - Correctly filtered to September 2026 Transport only: MYR30.49, 4 transactions.
   - Rows: 10.50 + 7.99 + 6.00 + 6.00 = 30.49.

13. **Unclaimed family reminder group fire — PASS**
   - “remind the family in 2 minutes: v056 unclaimed test”
   - Due reminder appeared in the family group at the correct time.

14. **September ledger integrity — PASS**
   - Explicit query “List every Food & Drink transaction in September 2026...” returned:
     - 2026-09-30
     - MYR15.67
     - “v05 family snacks”
   - This reconciles exactly to the September report’s Food & Drink total.

## 4. Confirmed live failures

### F1 — Targeted goal detail collapses to meaningless final answer
**Severity:** high

Prompt:
- “How much more do I need for my Europe fund?”

Observed:
- deferred acknowledgement
- final response: **“Done.”**

The read appears to fail in finalization/context/tool-result presentation rather than clearly reporting a domain error.

Please inspect the goal-list/detail tool path, discovery/ranking, final answer-only call, and any self-repair/result-discard interactions.

### F2 — Explicit quoted report context loses to recent Pocket Cash context
**Severity:** high

Flow:
- September 2026 PDF report exists in chat.
- User swipe-replied directly to that report message: “Send this as a csv”.
- Alex generated a **Pocket Cash CSV**, empty/header-only.

This is not a report generator defect: direct filtered PDF generation works. It is a reference/context binding defect. Explicit quoted/swipe-replied message must outrank recent/active unrelated context.

Please inspect provider message ID / quoted-message binding, report export source selection, remembered active context, and fallback ranking.

### F3 — Finance follow-up loses active reporting period/category
**Severity:** release-critical

Flow:
1. “Show me my september finance report” → correct September 2026 shared report:
   - total MYR639.78
   - transport MYR30.49
   - Food & Drink MYR15.67
2. “Show me all my expenses in september” → same correct September shared data.
3. “Breakdown the transport expenses” → correct September transport rows totaling MYR30.49.
4. “Give me a breakdown on food expenses next” → **wrongly returned October 1 food transactions**:
   - MYR18.50 lunch
   - MYR6.50 nasi lemak for lunch
   - total MYR25.00
5. Explicit “List every Food & Drink transaction in September 2026...” immediately returned the correct September 30 MYR15.67 snacks row.

Conclusion: stored ledger is healthy; multi-turn active period/category context is not reliably propagated.

Potential coupling with F2 should be investigated.

### F4 — Philips asset lookup fails while linked/original receipt retrieval succeeds
**Severity:** high

Known historical asset fixture:
- Philips Air Fryer / Philips HD9280/90
- Harvey Norman
- purchase date 27/09/2026
- MYR649
- serial V05-12345
- Visa ending 1234
- no verified warranty expiry stored

Prompts:
- “When did I buy my Philips Air Fryer, and when does its warranty expire?”
- Emoji-private retry

Observed:
- Alex claimed no Philips Air Fryer asset exists and offered to add it.

Isolation:
- “Show me my Philips Air Fryer receipt 😊” immediately returned the correct original Harvey Norman receipt.

Receipt provenance/media retrieval is healthy. Audit whether:
- asset row is absent due migration/data-path mismatch;
- asset exists but name resolution/search scope no longer finds it;
- receipt↔asset linkage is broken;
- natural asset aliases/model/serial are not searched.

Important safety requirement: Alex should not immediately offer to create a new asset until existing assets/linked receipts have been thoroughly checked, because this risks duplicate records.

Also preserve warranty grounding: do not infer an expiry from purchase date if no warranty duration/expiry is stored.

### F5 — Reminder clarification cannot complete after Alex itself asks for missing date/time
**Severity:** high

Flow:
- “Remind me to water the plants this weekend.”
- Alex correctly asked: “What day and time this weekend...?”
- User: “Saturday at 9 AM”
- Alex falsely claimed it **cannot create new reminders** and can only list/history.
- Swipe-replying directly to Alex’s clarification with “Saturday at 9 AM” produced the same false capability statement.

Direct complete reminder creation worked elsewhere, so this is continuation/tool-exposure/context-binding failure, not true missing capability.

### F6 — Voice pending lifecycle partially broken
**Severity:** high

Healthy core:
- safe defer/no trusted STT actions
- original audio preserved
- pending list exists
- numbered retrieval can work

Failures:
1. **No actual pinning** of pending voice note; only ⏳ marker/reaction visible.
2. Pending list showed times **8 hours early** (UTC instead of Asia/Kuala_Lumpur local time).
3. List should show **local date + time**, not only time.
4. Raw Media IDs/UUIDs are exposed in normal UX; they should be hidden.
5. “Play 1” / “Playing voice note 1 now.” can claim success with **no audio attachment**.
6. “Resolve 1” claimed success, but subsequent unresolved list still contained the item; resolution did not persist or mapping was wrong.
7. Swipe-reply “Listen to 1” and bare “1” could retrieve audio, showing the underlying media is intact.

Required canonical flow:
voice arrives → save original+provenance → PENDING → ⏳ + actual Alex-managed pin → numbered stable mapping → Play/Listen/Details/Resolve/Cancel all bind to exact pending item ID → playing does not resolve → explicit resolve/cancel or successful typed clarification marks terminal state → clears Alex marker/pin → original media remains retrievable.

### F7 — Post-due ordinary reaction incorrectly closes/unopens reminder
**Severity:** high; directly conflicts with newly locked accountability flow

The due group reminder “v056 unclaimed test” was reacted to with 👍 after due.

Then:
- “Is v056 unclaimed test still unresolved?”
- Alex replied: “I don't see any open reminders titled ‘v056 unclaimed test.’”

Therefore current post-due reaction semantics appear to treat an ordinary reaction as closure/ACK-terminal.

Required:
- post-due 👍/❤️/😂 = seen only; remain DUE_PENDING
- ✅ or explicit completion text = RESOLVED

### F8 — Cross-domain fallback contamination
**Severity:** medium

When F7’s reminder lookup failed, Alex unnecessarily searched saved notes and suggested a “Lighter location” note merely because it contained “v056”.

This was not a privacy leak (shared data), but reminder queries should not fall into unrelated saved-note search unless explicitly broadened by the user.

### F9 — Plain private-pool wording is misleading
**Severity:** medium/UX

Plain DM “How much do I have in pocket cash” correctly did not reveal private data, but wording said it could not find the pool. Emoji-private immediately returned MYR100.

Prefer scope-aware wording that does not expose private content but avoids implying data loss. Example concept: the normal query searched shared records; private search can be requested.

### F10 — Private browse presentation refinements
**Severity:** low/UX, but important at scale

Private saved-note list worked, but:
- date only; desired local date + time
- near-duplicate records clutter list
- duplicates must not be silently deleted; group/flag “similar saved notes” where appropriate.

## 5. New locked feature: private fuzzy discovery

Please evaluate the cleanest implementation that preserves the central privacy model.

Examples:
- “Do you remember me saying anything about keys?”
- “What private things have I saved?”
- “Show my private pictures.”
- “I saved something about the dryer but forgot what I called it.”

Normal query should search shared scope only. If nothing useful is found, Alex may offer a private search without revealing match existence. Owner can answer “Yes” to that explicit offer.

Direct “private” or emoji command should search owner-private immediately.

Do not create a parallel privacy engine if the existing centralized scope policy can support this safely.

## 6. New locked feature: claimed reminder accountability

Please inspect the existing reminder tables/state/events/reactions and propose the smallest durable extension.

Desired lifecycle:

`OPEN → CLAIMED → DUE_PENDING → RESOLVED`

side paths:
- `DUE_PENDING → NUDGE → ESCALATE_TO_INITIATOR`
- `CLAIMED/DUE_PENDING → RELINQUISHED → REOPENED → OPEN`
- `* → CANCELLED`

Requirements:
- claimant owns responsibility after claim;
- initiator remains escalation recipient;
- original group remains reopen destination;
- claimant due DM is pinned until resolution;
- ordinary post-due reaction is non-terminal;
- explicit completion is terminal;
- one logical reminder ID throughout;
- resolving/cancelling clears Alex-managed pin/marker only.

Please identify schema/migration changes, scheduler/outbox/reaction changes, and idempotency concerns.

## 7. What appears healthy and should not be redesigned

Please avoid turning this into a broad rewrite. v0.5.6 materially improved:
- centralized read privacy behavior in tested note/receipt/finance paths
- emoji-private group handoff
- descriptive read behavior for many tools
- leave re-add
- Saturday/calendar validation
- direct filtered PDF export
- receipt provenance
- pre-due family claiming
- safe voice defer policy
- shared vs owner-private note browsing

The goal is to repair the remaining mechanisms without regressing these.

## 8. Explicitly out of scope / not yet live-testable

- Priya’s WhatsApp number is not connected yet; true two-person cross-spouse live validation is pending.
- Home Assistant actionable mobile reminders currently report **0 Companion notify devices configured**. Do not classify missing HA action buttons as a v0.5.6 code delivery failure until device configuration is supplied.
- Voice transcription is intentionally not a trusted command path; do not “fix” voice by executing transcribed commands automatically.

## 9. Questions for Claude

Please answer these specifically:

1. Which of F1–F10 share a common root cause or context-selection mechanism?
2. Is F3 (finance month/category drift) caused by model context, tool ranking, report-state persistence, or missing deterministic binding?
3. Why can F2 ignore an explicit swipe/quoted report and use Pocket Cash context instead?
4. Does the Philips asset record disappear at migration/storage level, or only at retrieval/name-linking level?
5. Why does reminder clarification lose write capability after Alex asked for the missing fields?
6. Why do voice “Play 1” and “Resolve 1” diverge from the numbered list’s actual item mapping?
7. Which code path turns arbitrary post-due reactions into terminal reminder state?
8. Can private fuzzy discovery be added through the existing centralized scope policy without weakening privacy?
9. What is the smallest safe schema/state extension for claimed-reminder accountability?
10. What additional regression contracts would have caught these live failures before release?

Please also perform a quick scan for adjacent defects likely to surface from these same causes, even if not explicitly seen live.

## 10. Requested audit deliverable

Return:
- exact commit verified;
- complete-source access confirmation;
- tests/static checks run and results;
- finding-by-finding evidence classification;
- exact files/functions;
- root-cause grouping;
- repair order;
- minimum robust patch strategy;
- regression tests/contracts to add;
- any risks of changing currently healthy behavior;
- explicit limitations where live provider/production state cannot be reproduced.

Do not modify the repository. ChatGPT will review your findings and make the engineering decisions/repairs.

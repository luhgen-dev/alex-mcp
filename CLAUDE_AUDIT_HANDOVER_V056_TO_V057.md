# Claude Audit Handover — Alex MCP v0.5.6 Live Regression / v0.5.7 Repair

**Date:** 2 October 2026  
**Repository:** https://github.com/luhgen-dev/alex-mcp  
**Live-tested v0.5.6 baseline:** `af28a8182113f3cf5f132bd0e00934ea13dd7fe6`  
**Repair branch:** `repair/v0.5.7-live-regression`  
**Current repair head:** `12e8ad76f288c9867b3b9ce6eba7bb7a4750921f`  
**Draft PR:** https://github.com/luhgen-dev/alex-mcp/pull/20  
**Original detailed live-regression report:**  
https://github.com/luhgen-dev/alex-mcp/blob/repair/v0.5.7-live-regression/ALEX_MCP_0.5.6_live_regression_handoff.md

## 1. What I need from Claude

Please perform an **independent, read-only full-source audit** of:

1. the exact live baseline `af28a8182113f3cf5f132bd0e00934ea13dd7fe6`, and
2. the current repair head `12e8ad76f288c9867b3b9ce6eba7bb7a4750921f`.

The baseline is the code that produced the live failures. The repair head contains early proposed fixes and must **not** be assumed correct.

Do not modify the repository, PR, Home Assistant, or production data.

For each issue, classify your conclusion as:
- **DEMONSTRATED**
- **STRONGLY SUPPORTED**
- **SPECULATIVE**

Please cite exact files/functions and explain the mechanism. Recommend the smallest robust repair and the exact regression tests that should exist.

## 2. Why this handover is being sent now

Natural WhatsApp testing of v0.5.6 found several failures that the automated suite did not catch. I started tracing the code and made a few targeted changes on the repair branch, but before continuing I want a second independent engineering review so we do not patch symptoms and accidentally regress the working parts.

The current repair work is **incomplete and unvalidated**. No release decision should be made from the repair head yet.

## 3. Confirmed live failures from v0.5.6

### F1 — Targeted goal detail returns meaningless final answer

Prompt:
> How much more do I need for my Europe fund?

Observed:
- Alex showed the slow-working acknowledgement.
- Final answer became only: **“Done.”**

The goal read exists and the tool path appears present, so investigate:
- planning_goal_progress routing and exposure,
- final answer-only model round,
- tool-result presentation,
- whether a read result is being discarded or rendered as an empty generic completion.

### F2 — Swipe-reply to September PDF exports the wrong dataset

User swipe-replied directly to the September 2026 finance PDF and typed:
> Send this as a csv

Observed:
- Alex exported a **Pocket Cash CSV**, not the quoted September report.
- The CSV was essentially empty/header-only.

This is a serious deterministic context-binding failure.

Explicit WhatsApp quote/reply context must outrank recent conversational context.

Please inspect:
- `db.resolve_quoted_context`
- outbound `context_kind/context_id`
- attachment-message context persistence
- `_quoted_context_message`
- `report_export`
- active report context versus quoted report context

### F3 — Finance drill-down loses September and jumps to October

Live sequence:
1. September finance report → correct.
2. All September expenses → correct.
3. Transport breakdown → correct September rows, MYR30.49.
4. “Give me a breakdown on food expenses next” → incorrectly returned **October 1** food transactions totalling MYR25.
5. Explicit September Food & Drink query immediately returned correct September result: MYR15.67.

Conclusion:
- ledger/data is healthy;
- conversational report context is not deterministically carried into the next category drill-down.

Finance must remain source-of-truth quality: period/category/scope must not silently drift.

### F4 — Philips asset record cannot be found although receipt survives

Known historical fixture:
- Philips Air Fryer / Philips HD9280/90
- Harvey Norman
- purchase date 27/09/2026
- MYR649
- serial V05-12345
- no verified warranty expiry stored

Prompt:
> When did I buy my Philips Air Fryer, and when does its warranty expire?

Observed:
- Alex said it could not find the asset and offered to create one.

Isolation:
> Show me my Philips Air Fryer receipt 😊

Correct original Harvey Norman receipt was retrieved immediately.

Please determine whether the asset is:
- missing from persistent table/migration,
- present but filtered by scope,
- present but not matched by natural name/brand/model,
- disconnected from linked evidence.

Do not infer warranty expiry from purchase date unless warranty duration/expiry is actually stored.

### F5 — Reminder clarification asks correctly, then loses create capability

Prompt:
> Remind me to water the plants this weekend.

Alex correctly asked for day/time.

User:
> Saturday at 9 AM

Observed:
- Alex falsely said it cannot create reminders.
- Swipe-replying directly to Alex’s clarification with the same answer failed identically.

Direct complete reminder creation works, so this is a continuation/context/tool-exposure defect.

Please inspect:
- conversational follow-up detection,
- reminder-domain carry-forward,
- quoted Alex clarification context,
- mutation authorization from trusted current text,
- discovery/tool exposure cap.

### F6 — Voice pending lifecycle is inconsistent

Working:
- voice note is saved safely and not executed,
- original audio provenance is retained,
- unresolved list exists,
- numbered retrieval can work in some forms.

Failures:
- only ⏳ marker appeared; expected actual pin as well,
- displayed timestamps were UTC (8 hours early) instead of Asia/Kuala_Lumpur,
- list showed raw media IDs,
- “Play 1” sometimes claimed playback but sent no audio,
- “Resolve 1” claimed success but the item remained unresolved,
- swipe reply “Listen to 1” or bare “1” could retrieve the audio.

Required invariant:
`PENDING -> RESOLVED/CANCELLED`, with immutable original audio.

Playing/listening must never resolve.

Explicit typed clarification or resolve/cancel may resolve.

Pending item should show local date + time, human-readable numbering, no UUID/media ID.

### F7 — Ordinary post-due reaction closes an unclaimed reminder

An unclaimed Family Shared reminder fired correctly.

User reacted **👍** after due.

Then:
> Is v056 unclaimed test still unresolved?

Alex said it could not see an open reminder.

Current locked behavior is different:
- before due: any emoji can claim;
- after due: 👍/❤️/😂 = seen only, still unresolved;
- after due: ✅ or explicit “Done/Completed” = terminal completion.

Please inspect `services.claim_reminder_from_reaction` and all status transitions.

### F8 — Reminder lookup contaminates into saved-memory domain

When the reminder lookup above failed, Alex searched saved notes and surfaced an unrelated “Lighter location” note because it contained “v056”.

This is not a privacy leak, but it is wrong-domain fallback behavior.

Reminder queries should not fall into saved memory unless the user explicitly broadens the search.

### F9 — Plain private-pool wording falsely implies data loss

Plain DM:
> How much do I have in pocket cash

Correct privacy behavior prevented private exposure, but wording implied no pool existed.

Emoji-private:
> How much do I have in pocket cash? 😊

Correctly returned MYR100.

Desired behavior:
- preserve privacy boundary,
- explain that normal/shared scope did not include private records,
- offer private search without revealing private-result content.

### F10 — Private browse UX needs scale-safe refinement

Private saved-note browse works, but:
- display local date + time,
- do not display raw IDs,
- avoid clutter from near-duplicates,
- never silently delete duplicates.

## 4. Important live PASS behavior to preserve

Do not solve the above by broad redesign. These already passed live:

- full-day leave re-add after old tombstone bug;
- emoji-private receipt read from family group with automatic owner DM handoff;
- natural finance lookup: “What did I spend RM6.50 on yesterday?”;
- Saturday reminder date resolution;
- September PDF attachment and correct shared-only privacy;
- private Pocket Cash isolation;
- pre-due Family reminder claiming by emoji and claimant DM due delivery;
- safe voice defer/no trusted command execution;
- original voice audio preservation;
- private exact-match saved-note boundary;
- private saved-note browse;
- natural “Show me my saved notes” → Family Shared only;
- filtered September Transport PDF: MYR30.49 / 4 transactions;
- unclaimed family reminder due delivery to group;
- September Food & Drink ledger reconciliation: MYR15.67.

## 5. Privacy contract — must remain central

Current locked policy:

- Plain DM write → Family Shared by default.
- Plain DM read → Family Shared by default.
- Explicit word **private** or **any emoji in the current command** → owner-private.
- Explicit request for both shared+private may widen to all.
- Group never exposes private content.
- Private group request is acknowledged in group and fulfilled in owner DM.
- One spouse never sees the other spouse’s private data.
- The model must never widen scope on its own.

Please prefer the existing central `scope_policy` rather than creating parallel per-domain privacy logic.

## 6. New feature requirement — private fuzzy discovery

Owner should be able to ask naturally:
- “Do you remember anything I said about keys?”
- “Show me all my private saved notes.”
- “What private pictures have I saved?”
- “I saved something about the dryer but forgot what I called it.”

Plain/shared query still searches Family Shared only.

If no useful shared result is found, Alex may offer:
> I can also check your private records if you want.

A direct “Yes” to that offer authorizes that private follow-up.

Do not reveal that a private match exists before authorization.

## 7. New feature requirement — claimed reminder accountability

Target lifecycle:

`OPEN -> CLAIMED -> DUE_PENDING -> RESOLVED`

Side paths:
- `DUE_PENDING -> NUDGE -> ESCALATE_TO_INITIATOR`
- `CLAIMED/DUE_PENDING -> RELINQUISHED -> REOPENED -> OPEN`
- any state -> `CANCELLED`

Required semantics:
- claim before due by any emoji;
- claimant becomes responsible;
- due goes privately to claimant;
- claimant reminder is pinned until resolved;
- ordinary post-due reactions do not resolve;
- ✅ or explicit completion text resolves;
- first nudge claimant;
- later privately notify initiator;
- initiator may remind again, reopen to family, mark done, or cancel;
- claimant may relinquish and reopen;
- keep one logical reminder ID through lifecycle;
- Alex-managed pins must not disturb unrelated manual pins.

Please advise the smallest schema/state extension required.

## 8. Repair branch work already started — AUDIT THIS, DO NOT TRUST IT

Current repair branch head:
`12e8ad76f288c9867b3b9ce6eba7bb7a4750921f`

It is 5 commits ahead of the exact live baseline.

Files currently changed:
- `ALEX_MCP_0.5.6_live_regression_handoff.md`
- `alex-mcp/app/brain.py`
- `alex-mcp/app/ingress.py`
- `alex-mcp/app/mcp_server.py`

Commit sequence:
1. `236fbdde...` — add live regression handoff
2. `fe88067b...` — preserve report quote context and pending media state
3. `42b129e3...` — bind private-handoff report attachments
4. `81bc32d4...` — make finance drill-downs/exports context-stable
5. `12e8ad76...` — harden conversational context and read finalization

These changes are **not yet certified or live-tested**.

Please explicitly inspect whether they:
- genuinely fix root causes,
- introduce unsafe widening/mutation behavior,
- create hidden coupling,
- overfit to test phrases,
- weaken privacy,
- interfere with the four-call answer boundary,
- break attachment delivery or idempotency.

## 9. Areas I want a second opinion on

Please answer these questions directly:

1. Are F1, F2, F3 and F5 manifestations of one shared conversational-context/finalization problem?
2. Should finance drill-down context be model-carried, or persisted and bound deterministically below the model?
3. Is the repair branch’s quoted-report binding strong enough to guarantee explicit quote precedence?
4. Can the final-answer guard safely replace generic “Done.” when tool evidence exists without creating hallucinated answers?
5. Is the voice pending item model currently using the wrong selection namespace for `Play 1` / `Resolve 1`?
6. Is the pending-item auto-resolution in ingress too broad?
7. Is the WhatsApp pin control targeting the correct inbound/outbound message key?
8. Is F7 simply the current intentional ACK transition, or are there other scheduler paths that also make ACK effectively terminal?
9. Is the asset failure caused by retrieval logic or likely live-data absence?
10. What tests would most efficiently catch these failures before the next Home Assistant update?

## 10. Audit procedure requested

Please:

- confirm complete source access;
- verify both exact SHAs;
- read the full relevant source, not only snippets;
- run existing deterministic tests where possible;
- inspect PR #20 diff;
- reproduce with disposable/sandbox DB state where practical;
- do not touch real household data;
- do not modify the repository;
- identify any adjacent defect caused by the same mechanisms;
- avoid redesigning healthy subsystems unnecessarily.

## 11. Deliverable format

Return:

1. exact baseline SHA audited;
2. exact repair-head SHA audited;
3. full-source-access confirmation;
4. tests/static checks run and results;
5. root-cause groups;
6. F1–F10 verdicts with evidence classification;
7. assessment of the 4 repair commits after the report commit;
8. required repairs still missing;
9. regression tests/contracts to add;
10. risks to currently healthy behavior;
11. recommended repair order;
12. explicit limitations.

The objective is a concrete, minimal, durable v0.5.7 repair — not a redesign.

# Alex MCP v0.5.45 — Live smoke evidence and autonomous Dots repair handover

**Date:** 2026-10-22 (Asia/Kuala_Lumpur, UTC+08:00)  
**Repository:** `luhgen-dev/alex-mcp` — default branch `main`  
**Observed deployed baseline:** Home Assistant add-on `0.5.45`, merged to `main`; owner reported post-merge CI #1055 passing. `alex-mcp/config.yaml` identifies version `0.5.45`.  
**Source:** Owner's real WhatsApp / Home Assistant live acceptance session, with screenshots and exact text responses reviewed interactively. Screenshots are not committed (household/private content).  
**Results:** **53 targeted tests — 38 PASS / 15 FAIL**. A PASS generally means the observed WhatsApp workflow responded appropriately; only some cases received independent follow-up verification. It is **not** proof of unseen database writes or binary-level media fidelity.

> **Execution directive for Dots:** Take ownership end-to-end. Repair every discovered defect below **before** adding progressive teaching. Do focused root-cause analysis, reproduce in the existing External Alex Lab, implement minimal robust fixes, run targeted + existing regressions, review privacy/ACL/idempotency, build and verify the add-on/CI, and merge a tested repair PR when permitted. If a test or build breaks, investigate and repair it autonomously rather than returning partial work or asking for routine approvals. Only report completed work when objectively verified; if an external authorization/credential or physical Home Assistant installation is necessary, state the exact remaining user-only step and never claim it was performed. Do not pause arbitrarily or mask failed gates.

## 0. Protect the working foundation

Preserve **Family Shared / owner-private / spouse-private** ACLs, group @mention gate, quote/reply object binding, deterministic tools as source of truth, default MYR, pin+⏳ and ✅ lifecycle, payment-proof exactness, original media identity, task/plan/diary separation, task provenance, and short latency. In group, private results require owner DM. Spouse **Priya has not joined**: do not infer her actual WhatsApp delivery, consent, or ACL acceptance.

Read the current `README.md`, `alex-mcp/DOCS.md`, `alex-mcp/CHANGELOG.md`, `ALEX_LAB.md`, `BEHAVIOR_CERTIFICATION.md`, and existing tests as needed, rather than beginning another unfocused comprehensive audit. The External Lab runs on disposable state; use it before anything involving live HA data, WhatsApp delivery, or physical devices.

**Security:** This is a PUBLIC GitHub repository. Do **not** commit live names/phone numbers, household ledger records or account identifiers, screenshots, receipts, audio/video, actual leave consumption, exact personal roster snapshots, credentials, or diagnostic bundles with PII. Use invented people and synthetic fixture amounts in tests. Owner-supplied private work-profile truth will be transmitted separately to Dots through a private message and should be loaded into the owner's protected runtime data only.

## 1. Complete defect register (retain these stable IDs)

### Critical: correctness / unsafe cross-domain writes

| ID | Live evidence (test #) | Required fix and acceptance |
|---|---|---|
| GOAL-03 | #22 returned “updated target,” but #24 exposed **two separate DRAFT goals**, original target and new target. #22 is retrospectively FAIL. | Natural change by referenced goal performs **UPDATE of original identity**, no create/duplicate. Assert single row before/after, scope and target. Confirm success only after actual persisted update. |
| GOAL-01 | #23 natural activation falsely said “I don't have a tool”; explicit activation later succeeded #25. | Align advertised/exposed tools, NLU and runtime capability. Natural activation of uniquely identified goal reaches actual action. |
| GOAL-02 | #23 retry found ambiguous same-name goals and asked for raw ID. | Resolve by bound selected object, ownership, status/target and prior listed ordering; ask a human-readable clarification only if truly ambiguous. Never force internal DB IDs. |
| TASK-03 | #41 heads-up clarification reply lost the pending task + time, including when quoted. | Persist scoped, expiry-bound pending clarification with originating task ID, user message timestamp, intended reminder time, audience, quote and intent. “Just a heads-up” completes the **same** task-linked reminder. |
| TASK-04 | #41 failed clarification prompts were pinned as if durable reminders. | Only successfully created/scheduled actionable reminders receive pending pin/⏳, not failed questions. Clean obsolete pins deterministically. |
| TASK-05 | #43 and #46 unrelated questions kept resurrecting an unfinished “heads-up reminder” draft. | Pending state must **not hijack independent new requests**; explicitly offer resume only when relevant, expire/release/cancel orphaned draft and pins. Scope pending by sender/chat/object. |
| EAR-01 | #50 correctly summarised Ear group listening; #51 said saved; #52 could not recall it and returned unrelated umbrella note; #53 “most recent Ear session” routed to an unrelated saved-items list. | Ensure committed **durable Ear session, capture events, summary, decision/action state and privacy scope** on stop; prove write + indexed retrieval by Ear intent and group, independent of Tasks/Diary. Never claim “saved” before persistence. Explicit retrieval tests from fresh context and after restart. |
| MEDIA-01 | #34 actual WhatsApp voice note received, typed save and quoted retry both returned processing error; saved listing labelled a prior voice-note as **Note**, not preserved audio. | Save actual original audio bytes/reference/metadata by explicit typed/quoted instruction, independently of STT command authority. Retrieve playable exact asset, not placeholder text. Handle duplicate/retry idempotently. |
| MEDIA-02 | #35 original 27s WhatsApp video quoted save failed with same processing error. | Preserve original video attachment and playability (hash/binary identity where possible); exact tagged retrieval. Diagnose shared media pipeline without destructive migration. |
| PDF-01 | #5 “Send that as PDF” delivered a correct family expense total and six transactions, but included unrelated **goal/planning context** not in the displayed filtered dataset. | Export **exact frozen displayed finance view** with same scope/filters/date/rows/totals and no extra private or unrelated sections. Re-run rendered PDF content assertions and ACL checks. |

### User experience, privacy, and routing defects

| ID | Live evidence (test #) | Required fix and acceptance |
|---|---|---|
| NL-01 | #16 “I think I won't be able to make it to [appointment]” produced false “cannot cancel”; explicit “Cancel it” worked, and #17 confirmed disappearance. | Infer likely cancellation from indirect language but **clarify before destructive action if intent uncertain**. Never lie about supported diary cancellation; preserve exact appointment binding. |
| REM-01 | #28 quoted Family reminder “I'll handle this one” was misrouted to a new reminder-time question. | Recognise claim, bind quoted original reminder; transfer responsibility to owner DM (with proper pin/⏳). Do not start a new reminder. |
| REM-02 | #29 emoji reaction claim worked but the erroneous “What time should I remind you?” bot message from #28 stayed pinned. | Distinguish clarification from canonical reminder; clean stale incorrect pins when claim/resolve. |
| REM-03 | #32 and #33 relative future reminder confirmations only said “tomorrow 7:00 PM,” not explicit calendar date. | Always include resolved **local date, clock time and timezone** (e.g. Fri 23 Oct 2026, 7 PM MYT) using message-time anchor; preserve overlapping-reminder warning. |
| MEDIA-03 | #36 “show me saved videos” accurately said no videos but then listed unrelated notes/pictures. | Filter by requested media type, not a generic list; keep explicit scope and return useful empty result. |
| TASK-01 | #39 “original instruction” for existing task was fabricated as recent “Do I have any tasks?” | Render task's immutable original input/provenance, not current query. Never invent absent history. |
| TASK-02 | #39 task-history query instead enumerated unrelated reminders and claimed task missing from reminder list. | Keep Task detail/history and task-linked reminders as distinct but linked domains. Output original request, current state and chronological edits without raw IDs. |
| WORK-01 | #42 could not access roster and claimed no work-profile access despite v0.5.45 work-profile capability. | Trace work-profile routing, ACL, import/storage and work-profile tool exposure. Distinguish **profile not yet loaded** from falsely claiming feature unsupported. Once privately provisioned, answer shift and weekday/weekend OT per owner's effective-dated configuration. Do not hardcode personal data into public code. |
| WORK-02 | #43 claimed no access to leave entitlements; offered planned/taken leave only. | Support protected owner-specific entitled/used/remaining categories: Annual, Childcare, Medical, Wellness, Hospitalisation, DOIL. Explain unavailable source honestly; private input/import, validation, persistence, correct remaining arithmetic. |
| BRIEF-01 | #44 Daily Brief called 22 Oct 2026 “Wednesday” (actually **Thursday**). | Calendar date/weekday correctness in user-local timezone, test date boundary/DST as applicable. |
| BRIEF-02 | #44 brief said roster not configured. | Reuse Work-profile repair, display accurate private roster only in appropriate owner's DM; avoid leaking to Family. |
| BRIEF-03 | #44 brief showed one due task though #38 listed two open tasks. | Check brief's intentional due-only filtering vs actual bug; label counts clearly (due today versus all open). Do not assume all open tasks are due today. |
| BRIEF-04 | #45 ✅ on **second regenerated Daily Brief** did nothing; ✅ on original pinned brief removed pin and ⏳. | All versions/updates of one day's same-owner Daily Brief must link to a **canonical daily acknowledgement object**. React ✅ to ANY version -> acknowledge once idempotently, clear canonical pending pin/⏳. Retain linkage even if brief contents changed; preserve distinct dates/scope. |
| MENU-01 | #46 “Show me menu” routed to reminders instead. | Natural menu synonyms and `/menu` must route to Menu, not stale pending reminder domain. |
| MENU-02 | #46 explicit “show me your menu” listed only Reminders, Tasks and Daily Brief; omitted supported finance/shopping/goals/plans/media/Ear, etc. | Source functional menu from **actual enabled capabilities**, with useful numbered domain navigation and correct next-turn bindings. No invention of disabled features; no raw internal IDs. |

### New feature within this repair programme: durable Ear Discussion Journal

**Owner-approved requirement (derived from #47–53):** “Alex, show us our discussion(s)” must retrieve genuine **Ear** sessions in the same Family group. A list of multiple sessions should be numbered by recency, each with date/subject, participants/owners (when known), concise decision and progress status. “Show 1” must refer to this exact current list, not to earlier saved-media or Tasks results. Quote reply must preserve identity. Details show summary, proposed/changed/rejected, agreement, unresolved matters, actions, owner, due date when chosen, and audit history. The single actual storeroom fixture was: initially tentative weekend idea; decision for **Saturday 24 October 2026**, owner to sort shelves, no reminder/task/diary authorised.

**Suggested action/status vocabulary** (records with explicit state transitions, not just emojis): 💭 idea, ❓ decision pending, ⏳ agreed/action pending, 🔄 in progress, ⏸ on hold, ✅ done, 🚫 cancelled. “Read/acknowledged” must **NOT** equal “action completed.” Do not silently create Task, Diary, Plan or Reminder from a discussion; require consent before promoting/linking, and retain stable links if later promoted. Completed entries are searchable with history. Privacy: group shows Family Shared only; private discussions only in authorized owner DM. Spouse Priya has not connected; do not assert multi-user claim/permissions/delivery tests passed. Build synthetic multi-user authorization tests in lab and defer only unavoidable live spouse validation.

**Conversation examples to support:**
- “Alex, show us our discussions” -> numbered Family discussion journal.
- “Show 1” -> last Ear list's first entry, not saved photo #1.
- “What did we agree about the storeroom?” -> latest grounded matching decision.
- “I finished sorting the shelves” -> if unique, set action ✅, preserve source history; clarify if competing actions.
- “We haven't decided a date yet” -> mark ❓ without inventing a deadline.
- “Keep it just as a discussion; don't add to diary or task” -> save discussion, no side effects.
- “What's still pending from our discussions?” -> actionable, owner-scoped pending items and correct statuses.

## 2. Live test ledger (source-of-truth; do not inflate certification)

`P` = passed under the actual level of observation; `F` = failed. Tests marked P but with weak evidence must be reverified in lab after fixes.

| # | Result | Coverage / salient observation |
|---:|:---:|---|
| 1 | P | Messy wording “last bottle of detergent” -> Family shopping add. |
| 2 | P | “Actually need 2” -> reported quantity update. |
| 3 | P | “I bought detergent” -> reported completed, no unrelated media. |
| 4 | P | Shared monthly spend breakdown matched displayed category totals. |
| 5 | **F** | Correct PDF totals, extra unrelated goal/planning section (PDF-01). |
| 6 | P | Emoji/private wording returned only owner-private transactions. |
| 7 | P | Ordinary follow-up after private read returned Family only; no scope carry-over. |
| 8 | P | Invalid named private stash was rejected, no false success. |
| 9 | P | Family ledger still unchanged after failed stash request. |
| 10 | P | Private ledger still unchanged after failed stash request. |
| 11 | P | Exact requested shop/date/amount receipt photo returned. |
| 12 | P | Coffee correction reported; verified by #13. |
| 13 | P | Corrected Family total, still six rows, no duplicate. |
| 14 | P | Short “change that coffee back” MYR default and bound to right transaction (ack only). |
| 15 | P | Day Agenda returned genuine scheduled appointment, no visible draft/private leak. |
| 16 | P | Explicit cancellation succeeded; indirect phrasing first failed (NL-01). |
| 17 | P | Cancelled appointment absent from day's calendar. |
| 18 | P | Draft Plan created with no diary intent. |
| 19 | P | Plan finalised without reported Diary promotion. |
| 20 | P | Next 7 days Diary empty after Plan finalisation. |
| 21 | P | New goal created with initial target. |
| 22 | **F** | “Update goal target” created duplicate — proven by #24 (GOAL-03). |
| 23 | **F** | False inability to activate; ambiguity on retry (GOAL-01/02). |
| 24 | P | Listed two DRAFT goals, exposing #22 defect. |
| 25 | P | Activated selected new-target goal by “second / amount”, left other draft. |
| 26 | P | Cancelled old-target draft, preserved new-target ACTIVE. |
| 27 | P | Family reminder due exact time, pinned+⏳. |
| 28 | **F** | Quoted “I'll handle this one” treated as new reminder-time question (REM-01). |
| 29 | P | Emoji claim transferred Family reminder to private DM; orphan pin (REM-02). |
| 30 | P | “Can't do this, put it back in group” released claim **before due**. **Post-due release untested.** |
| 31 | P | Fresh Family-group claimable message, due preserved, pin+⏳. |
| 32 | P | Shared reminder for tomorrow 7PM created, pin+⏳; display date omitted (REM-03). |
| 33 | P | Second same-time reminder allowed with overlap warning and pin+⏳. |
| 34 | **F** | Original audio typed/quote save processing error (MEDIA-01). |
| 35 | **F** | Original video quote save processing error (MEDIA-02). |
| 36 | P | No saved videos reported; unrelated other media listing (MEDIA-03). |
| 37 | P | “Show number 2” returned actual intended image; binary fidelity not checked. |
| 38 | P | Two open Tasks listed numbered, no raw DB IDs. |
| 39 | **F** | Original Task instruction fabricated; reminder/task mix (TASK-01/02). |
| 40 | P | Date-only Task “remind in 30min” clarified heads-up vs set due-time. |
| 41 | **F** | “Just heads-up” lost quoted/pending state, failed/pinned questions (TASK-03/04/05). |
| 42 | **F** | Work roster falsely inaccessible (WORK-01). |
| 43 | **F** | Leave entitled/used balance falsely inaccessible; stale heads-up hijack (WORK-02/TASK-05). |
| 44 | **F** | Daily Brief wrong weekday; roster absent; due-task count needs grounding (BRIEF-01/02/03). |
| 45 | **F** | ✅ on regenerated brief ignored, original pinned brief ✅ succeeded (BRIEF-04). |
| 46 | **F** | “Show me menu” misrouted to reminders; explicit request incomplete (MENU-01/02). |
| 47 | P | Bare @mention activated Ear; text-specific natural activation not tested. |
| 48 | P | Normal group tentative idea got 👂 reaction, no silent task/reminder. |
| 49 | P | Later concrete decision got 👂, no unwanted action. |
| 50 | P | Bare @mention ended Ear, summarised agreement correctly, offered optional action. |
| 51 | P | Refused Task/Diary creation; Alex claimed discussion saved. |
| 52 | **F** | Could not retrieve previous Ear decision; unrelated note instead (EAR-01). |
| 53 | **F** | Exact latest Ear-session request returned unrelated saved notes (EAR-01). |

**Additional gaps that are not passed and must receive focused tests:** Successful named private-stash debit/expense **atomicity** (not just invalid stash no-write), screenshot-vs-database proof of reminder state, **post-due** release with fresh group message, due-time delivery to private DM after emoji claim, precise original audio/video retrieval, and household-specific work profile after private import. Do not mark these PASSED until validated.

## 3. Protected owner work-profile contract (do NOT put real owner values in this public file)

Implement an owner-private, effective-dated, versioned work-profile with:
- Weekly alternating Morning / Afternoon, overnight rollover across dates, weekend OT policy by current vs historical defaults, and user-explicit flexible overrides (not invented).
- Morning pre-shift OT distinct from normal duty; Afternoon after-shift OT crossing midnight.
- Saturday + Sunday possible Morning weekend OT; Saturday only Afternoon weekend OT. Different current vs previous default windows; an explicit owner-selected time can override a weekend.
- Six 2026 entitlement/used/remaining leave categories: Annual, Childcare, Medical, Wellness, Hospitalisation, DOIL. Entitled and used imported as authoritative numbers; remaining is computed; privacy is owner-DM only; don't conflate with Diary planned/taken leave.
- The owner's *actual* shift times, dated rotation anchor, effective historical weekend defaults and real leave values are in the **private Dots handover**. Do not derive them from screenshots, paste them into public issues, or silently overwrite protected live records.
- Daily Brief's Work section must read from this profile and handle versioned updates; show a truthful actionable “profile not configured” only when runtime profile truly absent.

## 4. Implementation & verification plan — autonomous; no new audit project

1. Freeze baseline evidence and reproduce reported failures with **synthetic** fixtures, unique correlation IDs and a structured defect-to-test matrix. No ad hoc duplicate workflows.
2. Trace domain routing/capability registry, pending/quoted references, mutation idempotency, stable object IDs, result verification, and pin lifecycle. Fix systemic causes shared across domains rather than papering over wording.
3. Fix **existing defects first**: goals atomic update/activation; finance exact PDF; saved-media original bytes; tasks/provenance/pending; reminder claim/pin/response date; work-profile/leave; Daily Brief; Menu; Ear persistence/retrieval. Sequence can be optimized by dependency, but none may be silently deferred.
4. Implement **Ear Discussion Journal** on top of proven persisted Ear sessions, with proper user/group ACL, numbered follow-up, status/action audit trail and no involuntary task creation. Exercise realistic conversation turns and competing-list cases.
5. **Only after current defects are fixed**, add progressive correction teaching as a fallback for *new* phrasings: WhatsApp swipe-reply to the mistaken Alex response; bind original quoted user request and object; immediately recover or ask a safe clarification; persist **approved contextual interpretation** with privacy scoping, versioning, collision review, and confidence/consent; route actual system faults to error reporting. Do not silently treat tentative/uncertain words as destructive orders. This is not model fine-tuning and must be bounded for latency/cost.
6. Run complete targeted deterministic regression + External Lab Tier A/Tier B, existing behavior certification and tests, independent-model reasoning gate where available and meaningful, image/add-on build, and fresh CI. Keep honest separation of simulated versus actual WhatsApp/HA tests. No inventing passing evidence.
7. Work on a dedicated branch; create reviewable PR(s), review own security/data migration risks and revertability, resolve CI, **merge** only after gates pass and permissions allow. Bump add-on version and update CHANGELOG/release documentation for deployability; avoid in-place changes to live HA data. Provide precise Home Assistant update/restart/recheck steps, and ask for user-side validation only where physically unavoidable.
8. Deliver final completion report with: merged commit/PR, released version, CI run/link, per-defect fix→test→result map, remaining live-only checks, migration/rollback notes and latency impact. If blocked by external authorization or unavailable HA hardware, do not claim full completion: clearly distinguish completed engineering deliverables from user-only acceptance, and take all other available work as far as possible first.

**No arbitrary cooldowns, ceremonial approvals or repeated status-check chatter.** A genuine blocked permission or provider imposed limit may require a user-only step. Never bypass GitHub branch rules, account authorization or security boundaries. Preserve red tests/evidence until repaired; fail closed rather than claiming success.

## 5. Hand-back conditions

- All **15 live FAILs** have targeted reproductions and a demonstrated implementation-level fix; incidental defects NL-01, REM-02/03, MEDIA-03, TASK-05, BRIEF-03 investigated too.
- No Family/private leakage; no stray finance writes or duplicate goal/expense mutations.
- Every Daily Brief version in a day shares one ack lifecycle, without cross-day ack confusion.
- Ear discussions survive process restart, support “show us our discussions”, numbered selection, drill-down, state updates, and preserve private/public separation.
- Work/leave available when privately configured, with correct rotation and leave arithmetic.
- Green project-specific gates + built HA add-on + versioned migration/release evidence.
- No false “I can't do that” for supported tools; no fabricated success when tool write fails.
- **Real HA / WhatsApp actions not executed by the Dots runtime are called out as pending user-side acceptance**, not falsely certified.

This document is the authoritative owner-approved v0.5.45 *live* repair scope. It does not supersede existing general project policies except where this session explicitly corrects work-profile details or prioritization.

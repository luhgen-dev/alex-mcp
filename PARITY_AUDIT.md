# Alex MCP Final Parity Audit

Audit date: 2026-09-28

Reference sources:
- Old Alex repaired release commit: `09edea2876b36560aa00a11a2b23085f34ce818c`
- `PHASE2_CONSTITUTION.md`, `PHASE2_ENGINE_STATUS.md`, `PHASE2_INTEGRATION_HANDOFF.md` at that commit
- Current Alex MCP main branch and regression suite
- Owner-approved MCP decisions: model-neutral brain, local Whisper/OCR first, text replies, deterministic privacy and tools, automatic receipt preservation separate from explicit Saved Memory

## Audit result

Alex MCP is architecturally sound and its current CI is green, but the audit found that the first MCP implementation was **not yet full user-facing parity** with the repaired old Alex/Phase-2 design. Do not call the product release-complete until the OPEN items below are either ported or explicitly retired by the owner.

## CLOSED / PRESENT

- Provider-neutral Grok / Gemini / OpenAI brain adapter
- MCP tools with authenticated actor hidden below model control
- Husband / wife identity and private/shared SQLite spaces
- Family Shared WhatsApp group plus private DMs
- Group channel restricted structurally to FAMILY_SHARED reads
- Text, image/PDF and voice-note ingestion
- Local Whisper-first multilingual transcription
- Local OCR plus selective model vision
- Original receipt/media preservation
- Receipt-to-ledger linking and exact original retrieval
- Recurring same-looking receipts remain distinct by event/media/date/reference
- Explicit Saved Memory remains separate from automatic receipt retention
- Append-only financial corrections
- Exact-decimal money storage and MYR/SGD no-guess rule
- Expense/income query and pending clarification recovery
- Shopping list
- Durable reminders to self/spouse/both and recurring reminders
- Goals, money buckets/stash values, leave balance
- Diary, Plans, combined Agenda, work roster and future leave lifecycle
- Work-conflict 1/2/3 ticket flow and PLANNED-leave choice
- Private-plan -> family selected-copy sharing
- Privacy-safe spouse availability result
- Deterministic cash-flow baseline excluding OT/variable cash
- Bounded Home Assistant entity lookup/state/control
- Durable outbound queue/retry, inbound idempotency/recovery
- Usage/tool audit and sanitized system diagnostics, including cached/reasoning/model-call telemetry
- Token-minimizing provider surface (maximum six relevant tools), low-reasoning default and bounded model-call loop
- Auto Saver provider routing: Gemini 3.1 Flash-Lite routine path → Gemini 3.8 Flash quality path → Grok/OpenAI resilience fallbacks
- Zero-token local replies for tiny greetings/health checks and per-provider cost telemetry
- Swipe-to-reply exact context binding and caption/quote/recent-instruction attachment pairing
- Family Shared explicit-mention/reply invocation gate
- Deterministic family/private finance and shopping read scopes plus voice-source finance filtering
- Plug-and-play HA configuration, QR pairing Web UI and family-group pairing command
- No Needle dependency

## PARTIAL — MUST CLOSE BEFORE RELEASE PARITY

1. **Recurring obligations / bill state**
   - Old Alex distinguishes expected, unconfirmed due, partial, paid, deferred, explicitly unpaid and cancelled.
   - MCP currently has finance + reminders, but no authoritative obligation lifecycle.

2. **Goal contribution / variable-cash history**
   - Old Alex keeps recurring goal baseline separate from one-off actual contributions and unallocated OT/bonus/refund cash.
   - MCP has goals/buckets and a baseline snapshot, but lacks full contribution/allocation/deviation history.

3. **Advanced work/OT rules**
   - MCP has dated roster + leave.
   - Old Alex also has repeating roster profiles, shift exceptions and OT offered/pending/planned/worked/unavailable logic plus absence eligibility. Exact OT pay formula remains intentionally unknown.

4. **Task policy history / quiet-presence behavior**
   - MCP reminder states exist.
   - Old Alex also keeps transition history, ACK follow-up policy, quiet hours/presence deferral and explicit monitoring delegation.

5. **Household library specialization**
   - MCP Saved Memory can retain/retrieve original files.
   - Old Alex additionally indexes household assets/manuals/warranties, expiry lookup, stable numbered disambiguation and soft removal.

6. **Diary conflict parity details**
   - MCP has the work 1/2/3 gate.
   - Still need existing-Diary overlap preflight, 48-hour ticket expiry, and the old explicit keep/shift/cancel choice when moving/cancelling events with linked reminders. Current MCP linked-reminder behavior is automatic and must be changed.

7. **Phase-3 simulator parity**
   - MCP has startup diagnostics and 29 deterministic regression tests.
   - It does not yet reproduce the repaired old Alex Phase-3 broad user-simulation/adversarial suite. A provider-backed MCP scenario harness is required before production promotion.

## INTENTIONALLY NOT A BLOCKER

- Alex voice replies: deliberately excluded by owner decision. As of v0.5.6, voice notes are preserved as a deferred private review inbox and never execute household commands until the user supplies typed clarification.
- Needle: deliberately excluded because it has no unique role in this architecture.
- Exact OT monetary formula: remains unknown until the owner supplies it; Alex must not infer it.
- External live travel/search/Google Sheets/TV presentation: connector-dependent extensions, not required for the first MCP household-core smoke.

## Next test name

The live Alex-MCP smoke test is named **MCP Check**.

MCP Check starts only after the PARTIAL parity items that affect household-core behavior are closed. It will test the new architecture end-to-end rather than repeat old implementation details.

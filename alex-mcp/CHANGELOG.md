# Changelog

## 0.5.28
Fixes from the first live v0.5.27 smoke test.
- Personal reminder confirmations now get their unresolved ⏳ + pin at creation. Root cause: a DM can arrive addressed as `<lid>@lid` while reminders store the owner's phone JID, so the confirmation was matched to its reminder by comparing chat-id strings and silently found nothing; markers only appeared later, when the reminder fired. The confirmation is now bound by the authenticated owner, never by chat-id spelling.
- "Mark <partial name> as done" (for example "Mark v0527 as done") now goes to the reminder tools, where the mutation-safe resolver asks which reminder you mean, instead of being swallowed by the shopping rule that also matches "mark ... done" (which leaked shopping-list output such as "Status filter: OPEN"). A word that belongs to a real shopping item still reaches shopping.
- Added regressions that reproduce both with a LID-addressed DM and the exact live phrase; both fail on v0.5.27 and pass here.

## 0.5.27
Reminder lifecycle repair after the v0.5.26 live WhatsApp smoke (independent Claude audit + fix).
- Natural relative answers such as "In about 10 mins", "in abt 40 mins", "in 40 minits", "in half an hour" and "2 hours from now" are now understood deterministically, with no AI call. Root cause of the repeated "What time should I remind you?": the date validator only accepted the exact form "in 10 minutes", so even a correct semantic-gateway paraphrase ("in about 10 minutes") was rejected. A relative duration inside a longer sentence ("bake for 40 mins at 7pm") still never overrides a stated clock time.
- The semantic gateway now asks the model for canonical date/time phrasing, and unusable gateway outcomes are audited so live failures can be diagnosed.
- Locked channel contract: a reminder created in MCP Home stays a claimable family reminder, including "remind me ...". Only an explicitly named other person, or explicit private/DM wording, moves it out of the group (private wording is handed to the owner's DM as before).
- One unresolved-marker rule for every reminder message: personal reminder confirmations, assigned and claimed reminder DMs, fired reminders and follow-ups. The current message of each unresolved reminder carries ⏳ + pin from creation/claim onward and hands them on when the reminder fires; 👍 (seen) keeps them; ✅/cancel clears both. Unclaimed family claim cards keep their existing ⏳ + pin.
- "Mark <reminder> as done" now reaches the reminder tools instead of shopping; reminder names are recognised from most of their words, never from one shared token.
- Reminder mutations are reference-safe: a single shared word (e.g. "v0526") can no longer select a different reminder, and "mark it done" / "cancel it" / "snooze it" can never be redirected to an already-closed reminder.
- Reminder listings put the reminder the user named first, then due, then upcoming; completed reminders are ordered by most recent completion with their completion time; the response reports total/shown/truncated so a capped list is never read as "not found".
- The private-DM slow acknowledgement now waits 12 seconds, so ordinary reminder turns no longer show "Sure, I'm working on that...".
- Added end-to-end regressions (real ingress/router/brain/MCP/outbox, only provider replies scripted with realistic imperfect arguments) for every v0.5.26 live failure; all of them fail on v0.5.26 and pass on v0.5.27.

## 0.5.26
Live semantic-gateway correction after the first v0.5.25 WhatsApp verification.
- Fixed the exact live failure where a reminder clarification reply such as “in abt 40 mins” could be correctly classified by the semantic gateway yet still be ignored by the normal Alex brain, causing Alex to ask “What time should I remind you?” again.
- The semantic gateway is now a real translator for the current reminder slot: when it returns a grounded high-confidence date/time normalization, that normalized answer is what the model sees for the current turn.
- The raw user-authored WhatsApp text remains the durable history/provenance and remains authoritative for privacy, scope, recipient, destination, object identity, and deterministic tool authorization.
- If the cheapest semantic route returns UNCLEAR/low confidence or malformed output, Alex may try the already-configured stronger semantic route once; no tools or database access are exposed and the call remains tightly bounded.
- Deterministic reminder validation is still the final authority before any reminder is written, so semantic normalization cannot invent or bypass missing AM/PM, dates, assignees, destinations, or ACL rules.
- Added a regression that reproduces the live swipe-reply path: pinned “What time should I remind you?” → quoted “in abt 40 mins” → normalized “in 40 minutes” → reminder creation, with the raw typo preserved in history and pending provenance.
- Release gate before publishing: core tests, structural self-test, Phase-3 stress, offline behaviour certification, reasoning corpus/snapshot/oracle, final parity audit, WhatsApp bridge contracts, Home Assistant container verification, and External Alex Lab all pass.

## 0.5.25
Semantic-gateway release for natural reminder clarification continuations.
- Added a bounded, tool-less semantic control interpreter for one already-grounded REMINDER_DRAFT when deterministic continuation parsing cannot confidently understand the user's wording.
- Preserved all proven deterministic reminder continuations as the first path, so understood phrases remain zero-token and unchanged.
- Covered the live failure family including “In 40 minutes”, typo/abbreviation variants such as “in 40 minits” and “in abt 40 mins”, and vague shorthand such as “tmr ard 7ish” without adding phrase-by-phrase execution logic.
- The semantic interpreter receives no tools, database/object IDs, conversation history, privacy authority, recipient/destination authority, or write capability; malformed, disallowed, unavailable, or low-confidence results fail closed.
- Semantic normalization is isolated to reminder date/time understanding only. User-authored trusted text remains authoritative for privacy, scope, assignee, destination, and object identity.
- Existing deterministic reminder validation still decides whether a due time is safe to persist, and pending reminder state is resolved only after the real reminder mutation succeeds.
- Added focused regression coverage for semantic-frame validation, bounded/tool-less payloads, zero-token fast paths, domain-switch anti-hijack, one-call continuation binding, routing/scope isolation, live shorthand failures, and create→resolve lifecycle.
- Release gate on the implementation PR passed core tests, structural self-test, Phase-3 stress, offline behaviour certification, human-AI corpus, frozen reasoning snapshot, deterministic reasoning oracle, final parity audit, WhatsApp bridge contracts, Home Assistant container/media verification, and External Alex Lab.

## 0.5.6
Live-regression repair release after the full 1 October v0.5.5 WhatsApp regression and complete independent source audit.
- Surfaced actionable MCP domain errors to the reasoning model instead of collapsing hundreds of normal validation/ambiguity failures into the same opaque tool error, eliminating blind retry loops.
- Hard-enforced the fourth/final model round as answer-only in code and defensively ignores non-compliant final-round tool calls, while preserving the four-call cap.
- Centralized read privacy around one normalized current-turn scope: ordinary reads are Family Shared; explicit private wording or any emoji selects the owner-private scope; Family Shared private intent hands off to the owner's DM without exposing private data in-group.
- Closed DM bulk/exact read scope leaks across finance, saved memory, shopping, tasks, assets and profile-backed reads while keeping plain DM writes Family Shared by default.
- Reworked Family Shared reminder claiming to the current owner contract: the setup message is claimable before due, first claim wins atomically, claimed reminders deliver at the original due time to the claimant DM, and reactions after a fired reminder acknowledge it rather than creating a late claim.
- Added unresolved Family Shared reminder pin/unpin handling and preserved explicit claimant handoff/accept/decline semantics for later two-person Priya validation.
- Added deterministic reminder date/time validation so missing exact times are clarified and weekday/date contradictions cannot be persisted (for example Saturday resolving to Sunday).
- Made reminder success wording auditable so phrases such as “I've set”/“noted” cannot falsely claim an uncommitted mutation.
- Repaired leave re-add after cancellation by safely reusing cancelled tombstones and preserving same-date update history; leave results now expose the previous state so replacement can be presented truthfully.
- Fixed natural finance interrogatives such as “What did I spend RM6.50 on today?” so they reach ledger reads instead of the write-ambiguity canned clarification.
- Preserved routed export tools when cash-pool hints are added, fixed compound report+cash requests, and made filtered finance exports an explicit supported path.
- Added durable report reply context and deterministic swipe-reply binding, including provider-message fallback shared with reaction handling.
- Grounded asset/warranty answers so unknown warranty expiry stays unknown and linked receipt/asset information can be synthesized without inventing a one-year warranty.
- Improved single-goal detail routing and goal-name normalization without changing working bulk goal listing.
- Made the Home Assistant Companion reminder bridge observable: missing device configuration and delivery errors are surfaced instead of hidden behind an unconditional “ready” message.
- Retired voice-note transcription as a trusted command interface. Every voice note is preserved with provenance, placed in a durable pending-review inbox, acknowledged politely, marked unresolved with the existing ⏳/pin mechanism, and executed only after typed clarification; the original audio remains retrievable afterward.
- Added pending-voice listing/retrieval contracts and kept multilingual typed English/Malay/Tamil/Tanglish reasoning intact.
- Expanded v0.5.6 regression coverage for the live failures and refreshed the 384-packet GPT-5.6 Sol reasoning snapshot plus deterministic reasoning oracle.
- Release gate at the repair head: core tests, structural self-test, Phase-3 stress, 136-contract/1135-variant offline certification with 0 failures, 384/384 real-AI reasoning snapshot, deterministic reasoning oracle, final parity architecture audit, WhatsApp bridge syntax, Home Assistant container/media build, and External Alex Lab all pass.

## 0.5.5
Go-live repair release from the v0.5.4 live smoke and full independent audit.
- Fixed the shared four-call orchestration bug: the final model call is answer-only, so a tool result can no longer be executed on the last round and then discarded behind a false "several tool steps" failure.
- Correctly classifies `finance_report` and `planning_list_cash_pools` as read-only, preventing discovery suppression, false mutation claims and finance-report fallback to the household snapshot.
- Hardened completed-attachment presentation so already delivered reports/files no longer say "shortly" or "couldn't finish" merely because the model exhausted the tool loop.
- Added deterministic private group handoff for stash plurals, owner pool names and private-only receipt/note matches without widening Family Shared ACLs.
- Added configurable household names/aliases (default Luhgen/Priya for this installation) so named reminder assignees resolve to the correct household user.
- Explicitly assigned reminders now route to the assignee DM regardless of command location unless the user explicitly asks for the family group, and the assignee receives an immediate private acknowledgement in human local time.
- Claimable Family Shared reminders keep atomic first-claim ownership, now send the successful claimant an immediate DM confirmation, preserve second-claim collision handling, and keep reaction removal non-releasing.
- Restored natural emoji-only private memory capture for retainable statements while excluding trivial chatter and preserving higher-priority domains; the control emoji remains stripped from persisted content.
- Restored plain natural leave read/cancel routing, named cash-pool balance reach, empty-bill terminal answers and whole-home status-card routing.
- Added Home Assistant Companion actionable reminder notifications for Android and iPhone as an additional delivery/control surface over the same Alex reminder state machine: Acknowledge, Done, Snooze 10m, Claim, and handoff Accept/Decline.
- HA notification actions use a durable local outbox, an outbound Home Assistant WebSocket subscription (no new inbound port), per-install signed action identifiers, idempotent HA event claiming, and the same atomic reminder claim/handoff functions used by WhatsApp.
- Stale HA actions cannot reopen completed/cancelled reminders; phone delivery, human acknowledgement and completion remain distinct states.
- Finance PDF visual redesign remains explicitly out of scope for this release and will be handled as a separate post-go-live side project.
- Existing canonical finance data, receipt ACLs, stash atomicity/migration, memory isolation, reminder claim/handoff transactions and additive upgrade safety remain preserved.


## 0.5.4
Live-smoke closeout release focused on the remaining routing, privacy, reporting, reminder-claim, stash and presentation regressions.
- Fixed Family Shared reminder reaction binding at the WhatsApp/provider-message boundary, preserved atomic first-claim ownership, and added a natural collision reply when a second household member reacts after the reminder is already claimed.
- Kept claimed shared reminders owned by the claimant and routed subsequent responsibility/follow-up to that person; reaction removal still never silently releases ownership.
- Fixed over-broad group-to-DM privacy handoff. Ordinary Family Shared finance and receipt reads now stay in the group unless the user explicitly requests private data or uses the emoji privacy shortcut.
- Made stored receipt scope authoritative: a receipt saved Family Shared from DM can be retrieved in the family group, while private saved/orphan media remains protected.
- Tightened receipt-only search so generic saved images/documents do not pollute receipt results.
- Preserved the emoji privacy shortcut as control metadata without storing the shortcut emoji as note/memory content.
- Tightened saved-memory semantic fallback so a forgotten exact memory is not replaced by a loosely related note and then described as an exact match.
- Fixed canonical finance report export context: PDF/CSV/JSON export now preserves the active finance report, including conversational follow-ups such as "send that as PDF", instead of silently falling back to the household planning snapshot.
- Added deterministic cash-pool discovery, single-pool resolution and atomic "create stash with opening balance" support. Genuine legacy stash/cash-pool buckets are bridged into the Phase-2 pool engine without treating allowances/reserves as stash.
- Locked stash/cash pools as an inherently owner-private domain: husband and wife pools stay in their respective private spaces, group stash questions hand off to the owner DM, and upgrade repair moves any accidentally Family-Shared cash pools back to the owner-private space without losing balance history.
- Post-repair independent audit closeout: aligned caption-only shared-receipt find/get predicates; kept finance exports in finance context across month changes and same-turn probes; restored natural rent/credit-card/car-loan due access to bills; preserved compound reply content while rewriting stale attachment-delivery wording; made deferred-document restart reconciliation non-blocking for normal sweeps; added canonical category aliases; hardened Home Status font discovery/clipping; and added claimant confirmation after accepted reminder handoff.
- Improved natural leave and reminder-due routing while keeping leave in Alex's own ledger without inventing an employer/HR integration or leave entitlement.
- Hid reminder UUID/state-machine/provider details from ordinary reminder-history presentation and added local human-readable diagnostic timestamps instead of raw UTC.
- Replaced stale "queued/shortly/on its way" wording for already-attached successful files with present-tense delivery wording; genuinely unresolved document retries keep the durable deferred lifecycle.
- Replaced the temporary terminal-style Home Status card with the locked deterministic premium renderer: landscape navy/charcoal layout, amber accents, two-column hierarchy and local Home Assistant state only.
- Added/expanded regressions for live reaction binding, second-claim collisions, Family Shared receipt retrieval, stash listing/opening balance, report-export context, memory isolation, attachment wording, Home Status PNG generation and presentation boundaries.
- Pre-release certification passed on the repair head: core tests, structural self-test, Phase-3 stress, 135-contract/1126-variant offline certification with 0 failures, 381-packet reasoning corpus with 0 routing gaps/forbidden exposure, refreshed GPT-5.6 Sol reasoning snapshot 381/381, deterministic reasoning oracle, final parity architecture audit, WhatsApp bridge syntax, Home Assistant container/media build, and External Alex Lab.

## 0.5.3
Upgrade-safety hotfix for existing Home Assistant installations.
- Fixed a startup migration ordering bug introduced in v0.5.2: an index on `leave_records.space_id` could be created before the new `space_id` column was added to an existing pre-v0.5.2 database, causing the add-on to exit during startup.
- The leave-scope index is now created only after the additive column migration and historical leave-scope backfill complete.
- Added an explicit regression test that boots a synthetic pre-v0.5.2 database, verifies existing leave data is preserved/backfilled, and verifies the new index is created successfully.

## 0.5.2
Post-live regression repair release after the third independent engineering audit.
- Fixed Family Shared reminder reaction claiming at the production ingress boundary; ordinary reactions remain quiet and claimable reminder reactions now resolve deterministically.
- Added durable claimant handoff: the current claimant can ask Priya/spouse to take a shared reminder, ownership changes only after the recipient accepts by DM reaction, and unreachable/pending handoffs never silently overwrite responsibility.
- Added a durable unresolved-document lifecycle: failed document delivery marks the original request with ⏳ and a pin, retries without blocking later commands, anchors the eventual attachment to the original request, and removes managed markers only after WhatsApp confirms SENT.
- Rebuilt monthly finance reporting around one canonical full-ledger dataset for WhatsApp, PDF, CSV and JSON; category totals are SQL-derived, privacy scope is never an expense category, and historical invalid privacy categories are cleaned without altering amounts/dates/scope.
- Added premium multi-page ALEX PDF presentation with navy/charcoal identity, amber accents, hierarchy, repeatable transaction headers, safe pagination and page numbers. Household/planning snapshots use the same professional document style.
- Separated actual monthly finance reports from broader household/planning snapshots.
- Fixed private receipt scope filtering and broadened safe Family Shared → owner-DM handoff for private reads/writes without widening group ACLs.
- Centralized the new-write scope contract across finance, shopping, memory, reminders, plans/tasks/diary, goals/planning, assets, work/leave and monitoring: normal new writes are Family Shared; explicit private wording or any emoji in the current trusted user command makes the write private; edits preserve stored scope.
- Added scope-aware leave lifecycle storage and migration. Existing leave remains owner-private; new leave follows the current scope rule; TAKEN leave materializes into the same scope.
- Fixed goal-contribution routing, natural leave/MC routing, plan-name confirm/update, reminder due shorthand, roster reads without optional OT configuration, local reminder-history timestamps, and exact saved-image “Yes/show it” continuation.
- Fixed Home Assistant one-shot monitor runtime import and moved one-shot HA-state checks to the scheduler loop instead of hourly polling.
- Prevented unsupported capability escape-hatches (fake HR portal/app/library instructions) and speculative diagnostic claims of broad instability.
- Added a global human presentation contract for meaningful lists/status/history/progress/breakdowns/reports while keeping simple confirmations concise.
- Added an undefined-name CI guard and expanded production-entry, scope, handoff, deferred-delivery and reporting regression coverage.
- Certification at the pre-version-bump repair head passed: 211/211 core tests, structural self-test, Phase-3 stress, 134-contract/1117-variant offline certification with 0 failures, 378-packet reasoning corpus with 0 routing gaps/forbidden exposure, refreshed GPT-5.6 Sol reasoning snapshot, deterministic reasoning oracle, final parity architecture audit, WhatsApp bridge syntax, Home Assistant container build/media verification, and External Alex Lab.


## 0.5.0
Final pre-HA release candidate after independent audit closeout.
- Broadened voice mutation detection for appointment/task/share/confirm flows while keeping normal clear speech frictionless.
- Local STT consensus now rejects conflicting critical numeric values such as amounts, times and numbered choices instead of guessing.
- Add-on restart recovery quarantines interrupted inbound message IDs and sends a durable verification notice rather than replaying a possibly completed mutation.
- Outbox commits each row before the next network send so a slow WhatsApp transport cannot hold SQLite's write lock across later sends.
- Expense confirmation now applies only to records genuinely awaiting human review.
- Failed WhatsApp media downloads are surfaced immediately and never become silent text-only turns.
- Audio replay uses push-to-talk only for OGG/Opus voice-note media; other audio formats remain normal audio attachments.
- Added deterministic regressions for the closeout fixes.

## 0.4.4
Repair release from the v0.4.3 live smoke test (Claude audit, cross-reviewed with GPT).
- **Voice = text.** A voice-note transcript is now the message itself, so voice gets exactly the same routing and tools as typed text (reminders, shopping, Home Assistant, saved items, finance). Original audio stays linked; `source=voice` finance filtering is unchanged.
- **No more cross-context leaks.** Voice notes never inherit an earlier text instruction; orphan pairing now applies only to captionless images/PDFs. Conversation history stores only the user's own words plus markers such as `[voice note]` / `[image attached]`, never OCR/PDF/transcript blobs.
- **Finance timestamps are deterministic.** New records use WhatsApp's send time unless the user states a time or a receipt supplies one; model-invented clock times are ignored. "Latest" ordering has a deterministic tiebreaker. Existing ledger rows are not modified.
- **Attachments.** The model is told when files are queued for delivery (no more "I can't send images" followed by the image), duplicate files are sent once, and a turn that found the requested file never ends with a failure message.
- **Saved items.** Browse everything ("what did I ask you to save"), filter pictures/documents/notes, word-level matching ("that vinyl thing", "my code word"), local dates instead of raw UTC. Space/ACL filtering unchanged.
- **Routing stopgap.** When the keyword gate recognises no domain for a real request, the model now gets the core read-only tools instead of none (fixes plural "expenses"/"transactions"). Small talk stays tool-light. Full facade routing arrives in 0.5.0.
- **Family group mentions.** @mention and swipe-reply detection now recognise WhatsApp LID identities as well as phone JIDs. Only the wake gate changed; privacy spaces are untouched. Unmatched group mentions log masked identity forms for diagnosis.
- **Phase-0 trace.** One compact local `_turn_trace` row per turn in the existing `tool_audit` table (source, tools exposed/called, model route, attachments, outcome). Local only, pruned after 14 days.
- Added 23 deterministic regression tests (`tests/test_v044.py`).

## 0.4.3
- Added **Auto Saver** as the recommended AI mode.
- Routine turns use Gemini 3.1 Flash-Lite first; harder visual/compound/planning turns start on Gemini 3.8 Flash.
- Grok 4.7 is retained as a resilience fallback, so existing xAI credit is used only when Gemini cannot complete the request.
- Automatic Grok fallback is capped at $0.50/month by default (configurable; 0 disables it) to prevent a Gemini outage from silently burning xAI credit.
- OpenAI remains an optional last fallback when configured.
- Provider fallback happens inside the same bounded MCP turn and preserves deterministic/idempotent tool protections.
- Tiny greetings, acknowledgements and Alex health checks now reply locally with zero model tokens and near-zero latency.
- Added per-provider/model usage breakdown to the Web UI so Gemini and Grok spend can be audited separately.
- xAI telemetry now uses the provider-reported exact billed request cost when available instead of estimating it from tokens.
- Added current Gemini 3.1 Flash-Lite and 3.8 Flash pricing for local budget telemetry.
- Kept local Whisper-first voice transcription and local OCR to avoid unnecessary multimodal API spend.
- Manual Grok/Gemini/OpenAI modes remain available and unchanged for explicit provider pinning.


## 0.4.2
- Reduced normal Grok reasoning from medium to low and capped provider orchestration at four model calls.
- Stopped replaying eight conversation turns into every self-contained request; history is now loaded only for genuine conversational continuation.
- Made runtime prompt context cache-friendlier by keeping ordinary requests date-stable and adding exact clock time only when relative timing requires it.
- Added cached-input, reasoning-token, model-call, tool-round and latency telemetry with Grok cached-input pricing support.
- Added a local 24-hour AI usage card in the Web UI; refreshing it makes no provider call.
- Fixed live MCP Check financial corrections being misclassified as diary/reminder/expense ambiguity.
- Added deterministic family/private finance and shopping read scopes plus voice/receipt/text finance-source filtering.
- Restored exact swipe-to-reply binding using persisted WhatsApp outbound message IDs; trusted replies can bind the exact referenced financial event.
- Restored caption > quoted reply > short same-sender attachment/instruction pairing.
- Restored Family Shared invocation gating: Alex responds only to an explicit @mention or a swipe reply to Alex.
- Restored exact numbered retrieval such as "Show 10".
- Added regressions for the above Smoke-3/Smoke-4 failures and aligned app/container/MCP versions.


## 0.4.1
- Fixed "Hi Alex, are you working?" being misrouted as a work/roster request.
- Added a live AI connection probe to the Home Assistant Alex MCP page.
- Added sanitized provider/runtime error classification without exposing API keys.
- Added persistent runtime diagnostics so MCP Check can distinguish provider failures from Alex internal failures.
- Added a 30-second provider timeout to prevent hung inference requests.


## 0.2.0
- Added shared/private shopping-list MCP tools with duplicate guarding.
- Added bounded Home Assistant entity lookup/state/control tools; sensitive domains are rejected deterministically.
- Added spouse/both-recipient durable reminders using configured WhatsApp numbers.
- Fixed voice-note media so audio does not force a private expense into the shared space.
- Switched money minor-unit conversion to Decimal/ROUND_HALF_UP semantics.
- Added recoverable failed/stale inbound processing without replaying completed messages.
- Hardened document prompt-injection guidance and selective image vision.
- Added local Tamil/Malay OCR language packs.
- Corrected the OpenAI default model id to gpt-5.6-luna.
- Enabled Home Assistant Core API access for the add-on.

## 0.1.0
- Initial independent Alex MCP architecture.
- Provider-neutral Grok/Gemini/OpenAI brain adapter.
- MCP v2 in-process tool layer with hidden authenticated actor context.
- SQLite/WAL finance, receipt, memory, reminder, goal and leave services.
- Automatic original receipt/media retention and local OCR.
- Local multilingual Whisper voice-note transcription with cloud fallbacks; text replies only.
- Automatic persistent Whisper model download/cache; no API tokens for normal voice transcription.
- Independent reminder scheduler and durable outbound queue.
- WhatsApp Baileys transport with HA Ingress QR pairing UI.
- Plug-and-play HA configuration fields for providers, keys and household phone numbers.
- Startup core diagnostics.

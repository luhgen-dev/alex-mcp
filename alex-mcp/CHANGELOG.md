# Changelog

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

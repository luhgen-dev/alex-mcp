# Changelog

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

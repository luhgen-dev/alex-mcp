# Changelog

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

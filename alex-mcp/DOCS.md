# Alex MCP

Alex MCP is a separate Home Assistant app. It does not modify the existing `alex-jarvis` installation or database.

## First install

1. Install the app from the Alex MCP repository.
2. Open **Configuration**.
3. Choose the AI mode. **Auto** is recommended: Gemini Flash-Lite handles routine turns, Gemini 3.8 Flash handles harder turns, and Grok/OpenAI are used only as configured fallbacks.
4. Enter the API keys you want Alex to use. For Auto mode, a Gemini key is the preferred primary key; adding Grok provides a resilient fallback.
5. Enter both authorised WhatsApp phone numbers in international format.
6. Save and restart the app.
7. Open **Web UI** and scan the WhatsApp QR from **WhatsApp → Linked devices → Link a device**.
8. When the page shows **Connected**, send Alex a normal WhatsApp message.

No API key, phone number, provider or pairing credential is stored in source code.

## What runs locally

- SQLite/WAL household records
- deterministic MCP tools
- privacy/identity checks
- reminder scheduler
- durable outbound queue
- original receipt/media storage and ACL-scoped later retrieval (including original voice notes)
- receipt OCR through Tesseract
- local multilingual Whisper voice-note transcription
- PDF text extraction
- WhatsApp session state
- startup structural diagnostics

The AI layer is used only for natural-language understanding, reasoning, tool selection and final wording. Normal household calls default to low reasoning and expose at most six relevant MCP tools. Short follow-ups inherit only the minimum safe domain context needed to resolve phrases such as “actually it was…” or “send me that again”; prior mutating actions are never replayed merely because they are in history.

### Auto Saver routing

Auto mode is designed for the best household experience at the lowest practical API cost:

- **Gemini 3.1 Flash-Lite** handles routine text/tool turns.
- **Gemini 3.8 Flash** is selected up front for visual input, compound requests and genuinely analytical planning; it can also take over if a Lite turn becomes unusually multi-step.
- **Grok 4.7** is a resilience fallback when configured, preserving existing xAI credit instead of spending it on every routine message.
- Automatic Grok fallback has a separate monthly safety cap (default **$0.50**). Set it to `0` to disable automatic Grok fallback entirely; manual Grok mode is unaffected.
- **OpenAI** is an optional final fallback when its key is configured.
- Simple greetings, thanks and Alex health checks are answered locally without any model call.
- Whisper and OCR remain local-first. Merely configuring a Gemini/OpenAI/xAI chat key does **not** upload voice audio. Optional cloud STT rescue is controlled separately by `cloud_stt_rescue_enabled` and is **off by default**.

The Web UI includes local 24-hour AI usage telemetry (input/cached/output/reasoning tokens, model calls, latency and cost) with a per-provider/model breakdown. Reading or refreshing this telemetry does not call an AI provider. xAI rows use xAI's provider-reported billed cost when the API returns it; other providers use the configured public token rates for local estimates.

Alex MCP also exposes a shared shopping list and a bounded Home Assistant tool surface. Home Assistant entity lookup/state reads are allowed; device writes are limited to explicitly requested low-risk actions on lights, switches, fans, climate and media players. Locks, alarm panels, covers, scripts, scenes, automations and other sensitive domains are rejected by the backend.

## WhatsApp conversation rules

In the bound Family Shared group, Alex responds only when explicitly @mentioned or when a household member swipe-replies to an Alex message. Ordinary family conversation is ignored. In private DMs, normal direct messages continue to work without an @mention.

Swipe replies are bound locally to the exact Alex message in the same conversation. For attachments, Alex uses caption first, then explicit quoted-reply context, then a short same-sender recent-instruction pairing when the attachment has no caption.

## Provider switching

Changing Auto ↔ Grok ↔ Gemini ↔ OpenAI is an app setting. The MCP tools and household database do not change. Manual provider modes intentionally disable automatic cross-provider fallback so you can pin Alex to one provider when testing.

## Voice notes

As of **0.5.6**, voice notes are a safe deferred-review inbox, not a trusted command interface.

When Alex receives a voice note it:

- preserves the original audio and WhatsApp provenance locally;
- acknowledges the message without executing finance/reminder/diary/task/home actions from speech;
- creates a private pending item for the authenticated sender;
- keeps the item visibly unresolved with the managed ⏳/pin lifecycle;
- asks the user to type the intended instruction when convenient.

You can later swipe-reply to the original voice note or Alex's pending acknowledgement and type the clarification. Alex binds that typed instruction to the saved voice item, processes the typed request normally, marks the pending item resolved, and clears the managed unresolved markers. The original audio remains retrievable for provenance.

You can also ask naturally:

- "Show me my unresolved voice notes."
- "What voice notes are still pending?"
- "List my recent voice notes."
- "Send me the original voice note again."

Typed language remains fully model-assisted: English, Malay, Tamil, Tanglish/transliteration and natural mixtures are still accepted. Alex continues to reply in English unless another output language is explicitly requested.

Legacy STT configuration fields remain accepted for upgrade compatibility, but normal 0.5.6 voice-note command execution does not depend on transcription and does not upload the audio to an AI provider.

Alex replies in text only.

## Receipts

Every incoming image/PDF is stored before AI reasoning. When it represents a financial transaction, the ledger tool links the original media to the transaction. Explicit "save/remember this" memory is separate from receipt retention.

Use ordinary language such as:

- "How much did I pay TNB last month?"
- "Show me the receipt for that payment."
- "Remind me Wednesday at 9am to pay electricity."
- "Save this for me."
- "How much did I spend over the weekend?"
- "How much did the family spend yesterday?"
- "Show my voice expenses."
- "List my recent voice notes."
- "Send me the original voice note again."
- "Show 10." (after Alex displayed a numbered result list)
- "Add detergent to the shopping list."
- "Remind my wife Friday at 9am to renew road tax."
- "Turn off the living room light."

## Privacy

The model never chooses an identity or private space. Authenticated actor context is injected beneath MCP and omitted from the tool schema visible to the model.

## Data

Persistent data lives under the app's `/data` volume:

- `alex_mcp.db`
- `media/`
- `whatsapp_auth/`
- `selftest.json`
- `models/` (local Whisper model cache)

Original household media and their selected transcripts/OCR are retained intentionally for provenance and later retrieval; Alex does not silently prune them. Plan storage capacity accordingly and take a full Home Assistant backup before upgrading or migrating the app.

## Home Assistant actionable reminder notifications

Alex can mirror reminder alerts into the Home Assistant Companion app on Android and iPhone. Home Assistant is an extra delivery/control surface only: the Alex reminder row remains the single source of truth.

Configure **ha_notify_devices** with one entry per household phone:

- **id** — a short local label such as `luhgen_phone`
- **owner** — `husband` or `wife`
- **notify_service** — the Home Assistant notify service without the `notify.` prefix, for example `mobile_app_galaxy_s26_ultra`
- **active** — true/false

Assigned reminders can produce an immediate phone acknowledgement and the due notification exposes **Acknowledge**, **Done** and **Snooze 10m**. Claimable Family Shared reminders expose **Claim**. Reminder handoff requests expose **Accept** and **Decline**.

Buttons on Android/iPhone and WhatsApp reactions converge on the same atomic Alex reminder state. Notification delivery alone never means the person acknowledged or completed the reminder.

The Companion action bridge connects outbound to Home Assistant's WebSocket API; it does not open a new inbound port. Action identifiers are signed with an install-local secret stored under `/data`, and Home Assistant event IDs are claimed idempotently before any reminder state change is applied.

If no `ha_notify_devices` entries are configured, normal WhatsApp reminders continue unchanged.


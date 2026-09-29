# Alex MCP

Alex MCP is a separate Home Assistant app. It does not modify the existing `alex-jarvis` installation or database.

## First install

1. Install the app from the Alex MCP repository.
2. Open **Configuration**.
3. Choose the AI mode. **Auto** is recommended: Gemini Flash-Lite handles routine turns, Gemini 3.8 Flash handles harder turns, and Grok/OpenAI are used only as configured fallbacks.
4. Enter the API keys you want Alex to use. For Auto mode, a Gemini key is the preferred primary key; adding Grok provides a resilient fallback.
5. Enter the authorised WhatsApp number(s) you want active in international format. The spouse/second number may remain blank until you intentionally enable it.
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
- original receipt/media storage
- receipt OCR through Tesseract
- local multilingual Whisper voice-note transcription
- PDF text extraction
- WhatsApp session state
- startup structural diagnostics

The AI layer is used only for natural-language understanding, reasoning, tool selection and final wording. Alex v0.5 presents the model with a stable **14-tool provider facade** and exposes at most six relevant schemas on an ordinary turn. Detailed deterministic MCP tools stay behind that facade and are loaded only through a bounded specialist pack when needed. This reduces tool-choice drift without moving privacy, arithmetic, writes or household truth into the model.

Short follow-ups use actor-and-chat-scoped conversational focus containing stable object IDs only. The underlying MCP tool re-checks authorization whenever an ID is dereferenced. Explicit requests such as "save the next picture as wedding invitation" create a single-use three-minute attachment focus; unrelated chats/users cannot consume it.

### Auto Saver routing

Auto mode is designed for the best household experience at the lowest practical API cost:

- **Gemini 3.1 Flash-Lite** handles routine text/tool turns.
- **Gemini 3.8 Flash** is selected up front for visual input, compound requests and genuinely analytical planning; it can also take over if a Lite turn becomes unusually multi-step.
- **Grok 4.7** is a resilience fallback when configured, preserving existing xAI credit instead of spending it on every routine message.
- Automatic Grok fallback has a separate monthly safety cap (default **$0.50**). Set it to `0` to disable automatic Grok fallback entirely; manual Grok mode is unaffected.
- **OpenAI** is an optional final fallback when its key is configured.
- Simple greetings, thanks and Alex health checks are answered locally without any model call.
- Whisper and OCR remain local-first; Auto mode does not start sending every voice note or receipt directly to a paid multimodal model.

The Web UI includes local 24-hour AI usage telemetry (input/cached/output/reasoning tokens, model calls, latency and cost) with a per-provider/model breakdown. Reading or refreshing this telemetry does not call an AI provider. xAI rows use xAI's provider-reported billed cost when the API returns it; other providers use the configured public token rates for local estimates.

Alex MCP also exposes a shared shopping list and a bounded Home Assistant tool surface. Home Assistant entity lookup/state reads are allowed; device writes are limited to explicitly requested low-risk actions on lights, switches, fans, climate and media players. Locks, alarm panels, covers, scripts, scenes, automations and other sensitive domains are rejected by the backend.

## WhatsApp conversation rules

In the bound Family Shared group, Alex responds only when explicitly @mentioned or when a household member swipe-replies to an Alex message. Ordinary family conversation is ignored. In private DMs, normal direct messages continue to work without an @mention.

Swipe replies are bound locally to the exact Alex message in the same conversation. For attachments, Alex uses caption first, then explicit quoted-reply context, then a short same-sender recent-instruction pairing when the attachment has no caption.

## Provider switching

Changing Auto ↔ Grok ↔ Gemini ↔ OpenAI is an app setting. The MCP tools and household database do not change. Manual provider modes intentionally disable automatic cross-provider fallback so you can pin Alex to one provider when testing.

## Voice notes

Voice notes are transcribed before the conversational model sees the request. With `stt_provider=auto`, Alex uses **local multilingual Whisper first**. This uses no AI API tokens and is the preferred path for Tamil/English/Tanglish voice notes.

The default local model is `base`. It downloads automatically on the first voice note and is then kept under persistent `/data/models`. You can select `small` in Configuration later if you want to trade more storage/RAM for harder multilingual transcription.

v0.5 no longer treats every non-empty auto-language transcript as trustworthy. Suspicious/low-signal local output gets a second local English hypothesis; obvious repetitive/hallucinated output may fall through to a configured cloud STT provider in `auto` mode. The normalized transcript then receives the same provider-facing capabilities as typed text. Alex also retries once rather than falsely claiming a clearly routed action is unsupported, and user-facing replies remain English unless translation is explicitly requested.

Alex replies in text only.

## Receipts

Every incoming image/PDF is stored before AI reasoning. When it represents a financial transaction, the ledger tool links the original media to the transaction. Explicit "save/remember this" memory is separate from receipt retention. Repeated monthly receipts remain separate evidence objects even when merchant, bank and amount are identical; date/reference/media identity remains available for later retrieval.

Use ordinary language such as:

- "How much did I pay TNB last month?"
- "Show me the receipt for that payment."
- "Remind me Wednesday at 9am to pay electricity."
- "Save this for me."
- "How much did I spend over the weekend?"
- "How much did the family spend yesterday?"
- "Show my voice expenses."
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

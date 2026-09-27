# Alex MCP

Alex MCP is a separate Home Assistant app. It does not modify the existing `alex-jarvis` installation or database.

## First install

1. Install the app from the Alex MCP repository.
2. Open **Configuration**.
3. Choose the AI provider. The initial default is **Grok**.
4. Enter the matching API key.
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
- original receipt/media storage
- receipt OCR through Tesseract
- local multilingual Whisper voice-note transcription
- PDF text extraction
- WhatsApp session state
- startup structural diagnostics

The chosen AI is used for natural-language understanding, reasoning, tool selection and final wording.

Alex MCP also exposes a shared shopping list and a bounded Home Assistant tool surface. Home Assistant entity lookup/state reads are allowed; device writes are limited to explicitly requested low-risk actions on lights, switches, fans, climate and media players. Locks, alarm panels, covers, scripts, scenes, automations and other sensitive domains are rejected by the backend.

## Provider switching

Changing Grok ↔ Gemini ↔ OpenAI is an app setting. The MCP tools and household database do not change.

## Voice notes

Voice notes are transcribed before the conversational model sees the request. With `stt_provider=auto`, Alex uses **local multilingual Whisper first**. This uses no AI API tokens and is the preferred path for Tamil/English/Tanglish voice notes.

The default local model is `base`. It downloads automatically on the first voice note and is then kept under persistent `/data/models`. You can select `small` in Configuration later if you want to trade more storage/RAM for harder multilingual transcription. If local transcription fails, configured cloud transcription providers are fallback options.

Alex replies in text only.

## Receipts

Every incoming image/PDF is stored before AI reasoning. When it represents a financial transaction, the ledger tool links the original media to the transaction. Explicit "save/remember this" memory is separate from receipt retention.

Use ordinary language such as:

- "How much did I pay TNB last month?"
- "Show me the receipt for that payment."
- "Remind me Wednesday at 9am to pay electricity."
- "Save this for me."
- "How much did I spend over the weekend?"
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

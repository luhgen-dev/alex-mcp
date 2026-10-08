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

### Provider circuit breaker (0.5.33)

If a provider fails provider-wide (quota or rate limit, sign-in/key rejected, billing block, outage or connection error), Alex skips it for a short cool-down (1–15 minutes depending on the failure) so the next messages go straight to a working fallback instead of repeating the failed call first. If every configured provider is cooling down, Alex still tries them all, so it is never left with no route. A successful **Test AI now** re-opens a cooled-down provider immediately.

## ChatGPT plan (0.5.33, off by default)

Alex can use your own ChatGPT subscription through OpenAI's "Sign in with ChatGPT" plan usage for self-hosted apps, so those calls draw on your plan's allowance instead of paid API credit. Nothing changes until you both sign in **and** choose a mode.

**Sign in (from your phone is fine):**

1. Open the Alex MCP **Web UI** → **ChatGPT plan** → **Start sign-in**.
2. Tap **Open ChatGPT sign-in**, sign in and allow Alex to use your plan.
3. Your browser then lands on a page that cannot load (`127.0.0.1:1455/...`). That is expected: copy the whole address from the address bar.
4. Paste it into the panel and tap **Finish sign-in**, then **Test ChatGPT**.

The link is valid for 15 minutes. The session is stored only in `/data/chatgpt_plan/` (owner-only file permissions). It is never written to the configuration, logs, database or repository, and Alex refreshes it automatically whenever it uses ChatGPT. If Alex doesn't use ChatGPT for more than 30 days (for example the mode is `off` or the app is stopped), or you disconnect Alex in ChatGPT settings, the panel asks you to sign in again. **Sign out** in the panel forgets the local session.

**Modes** (Configuration → **ChatGPT plan mode**):

- `off` — default; not used.
- `shadow_only` — only the AI router comparison below uses ChatGPT. Every reply is still produced exactly as before.
- `primary` — ChatGPT answers text messages first; your API providers stay as automatic fallbacks (for example when the plan's usage limit is reached). Photo/document turns keep the existing visual route first, with ChatGPT as the first fallback.

**ChatGPT plan model** is optional; leave it empty to use the first model your plan offers (the panel lists them after sign-in).

Notes: on ChatGPT Plus, Alex shares your plan's rolling usage limit with your own ChatGPT/Codex use. Plan usage is recorded in the usage card at $0 API cost and does not count toward the optional monthly API budget guard.

### Semantic router (0.5.36, off by default)

Alex has 121 tools but intentionally keeps the provider surface small. The semantic router lets the signed-in ChatGPT plan model read natural, typo-heavy household wording against the **full tool catalogue** and return strict structured JSON:

- ordered real tool names;
- a short intent such as `CREATE_REMINDER` or `READ_FINANCES`;
- reference kind (`NONE`, `ACTIVE`, `QUOTED`, `LATEST_LIST`);
- grounded slots such as date/time, relative minutes, item numbers, target, scope, amount/currency, name/query/action;
- confidence and whether clarification is genuinely needed.

Configuration → **Semantic AI routing**:

- `off` — no live semantic routing. Existing deterministic keyword routing remains the reply path.
- `shadow` — the same semantic interpreter runs in the background after the reply and is logged for comparison. It never changes the reply or calls a tool.
- `live` — the same interpreter runs before the brain. Its safe tool picks are placed first, keyword-routed tools stay behind them, and a high-confidence structured interpretation is supplied to the brain only as a **non-authoritative language hint**.
- `on` — legacy v0.5.34/v0.5.35 value; automatically treated as `live` so existing installations upgrade without a configuration edit.

This setting is independent of **ChatGPT plan mode**. For example, you can keep ChatGPT plan mode on `primary` so GPT-6 Luna answers Alex's text turns, while Semantic AI routing is set to `shadow`. Moving `shadow → live` later is only a Configuration change plus restart; it does not require another code release.

Safety remains deterministic in every mode. The semantic model cannot grant write permission, privacy scope, recipient access or tool authorization. Alex still checks the original trusted user text, quote binding, block lists, actor/ACL rules and deterministic tool validation. If the router fails, times out after 6 seconds, returns unusable output or is paused after repeated failures, Alex falls back to the existing keyword route for that message.

The Web UI **Semantic router** card shows recent structured interpretations, tools, slots, confidence and median router latency. Rows are kept for 30 days. Photo, document and voice-note turns and plain chit-chat are not live-routed.


## Architecture direction: thin Alex + specialist local services

This is the locked direction for future expansion. Alex should remain the household-facing orchestrator rather than becoming the place where every domain stores, calculates, monitors and renders its own data.

**Alex owns:**

- authenticated identity, privacy/ACL enforcement and trusted context;
- natural-language understanding and safe intent/tool routing;
- orchestration between specialist services;
- WhatsApp conversation, progress/acknowledgement messages and final delivery.

**Specialist local services own:**

- domain-specific authoritative storage where appropriate;
- deterministic calculations, statistics and background monitoring;
- integration with external/local data sources;
- report-ready structured data.

A specialist service may run as its own Home Assistant add-on/container on the same Mini PC with explicit CPU/memory limits. Do not split every small feature into a microservice: create a subsystem only when a domain is substantial enough to benefit from independent storage, background work or deterministic computation. Alex must continue to work as the front door even as these services grow.

### Shared report renderer

PDF/image generation is a separate reusable subsystem, not work for the Alex brain and not a different renderer per domain.

The report path is:

`Alex request -> domain service -> trusted structured data -> report service -> fixed/versioned template -> PDF/PNG -> Alex -> WhatsApp`

Rules:

- Templates are deterministic and versioned (for example `expense_monthly_v1`, `fitness_monthly_v1`, `home_status_card_v1`).
- Re-running the same template for another month changes the data, not the design.
- Luna may help understand the request, but it does not invent the report layout on every run.
- The renderer should prefer a free/self-hosted existing engine behind a small adapter. A Chromium/HTML renderer such as Gotenberg is the current leading candidate, but the architecture must not depend on one vendor before real deployment testing.
- The same renderer should serve the pending **Expense PDF** and **Home Status Card**, and later Fitness/Health reports and other domains.
- Heavy rendering may run asynchronously; Alex can acknowledge the request and deliver the finished file when the report service returns it.

### Fitness/Health subsystem — next major direction

Fitness/Health is the highest-priority planned subsystem after the current production validation.

Its first responsibility is an authoritative **Alex workout ledger**, independent of Samsung Health:

- workout programmes/templates and numbered exercises;
- active workout sessions;
- sets, reps, weights and exercise-specific fields;
- corrections, notes, skipped exercises and completion state;
- workout history, progression, PRs and deterministic statistics.

WhatsApp is the primary logging interface. A user should be able to start a workout, receive the numbered template and log terse follow-ups such as `1, 80kg, 12 reps` without retyping exercise names. Planned data and performed data must remain distinct and traceable.

Samsung Health is an enrichment/presentation ecosystem, not the sole workout source of truth. The preferred bridge is Samsung Health -> Android Health Connect -> Home Assistant/local Fitness service where supported. Health data such as sleep, weight/body composition, heart rate/HRV, steps and other available measurements can be retrieved on demand or used for wellness-oriented summaries. Alex must not diagnose medical conditions or invent missing measurements.

The Fitness service should perform background analytics independently of Alex and expose a small deterministic tool/API surface. Alex fetches results when asked. Future reports use the shared report renderer rather than adding a fitness-specific PDF engine.

If bidirectional Health Connect/Samsung Health writing is later added, Alex's own ledger remains the complete authoritative workout record until real-device testing proves exactly which fields Samsung Health preserves and renders.

### Lower-priority future integration: marketplace order watcher

The previously discussed Shopee/Lazada/TikTok Shop order watcher remains a useful future idea but is lower priority than Fitness/Health. The intended experience is: identify an order from user-supplied proof such as an order screenshot, retain the marketplace/order identity, monitor status through a reliable authenticated source when feasible, then switch to courier tracking once available. Do not depend on phone notifications as the primary source and never guess an order state when the marketplace cannot be checked.

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
- `chatgpt_plan/` (only if you sign in to the ChatGPT plan; owner-only permissions)

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


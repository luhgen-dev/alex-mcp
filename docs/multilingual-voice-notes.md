# Multilingual voice notes and selective API credit use

Implementation review: 2 November 2026, based on main `ee92037` (0.5.52).
Release 0.5.53 packages the opt-in pilot; automated validation is not evidence of live WhatsApp speech accuracy.

## What stopped the old path

- `media.process_payload_media` saved audio without transcription; ingress always returned the deferred-voice inbox response. The brain prompt explicitly forbade executing transcripts. Replacing the old STT model alone could not change that behavior.
- Dormant local STT compared multiple decodes and used language/English-intent heuristics. The new path does not revive those comparison passes or their cloud rescue chain.
- The Tamil fallback could fill the entire tool budget with unrelated read tools, leaving no discovery tool. The discovery mutation gate recognized English action words only.
- Persistent teaching explicitly rejected `source='voice'`. Reminder time validation principally recognized English evidence. These downstream restrictions needed repair as well as transcription.

## Implemented path

Authenticated, addressed WhatsApp audio → preserve original → bounded local Ogg/Opus-to-WAV conversion → one `gpt-transcribe` request → original-language user text → existing Alex brain, scoped tools, database and outbox.

No translation model or new conversational brain is added. Tamil, English and Malay are transcription hints; code-switching stays intact. Local conversion does not recognize speech. The legacy `stt_provider` option remains for old diagnostic callers but does not control this new path.

The same brain uses existing capability discovery for unfamiliar multilingual wording. A conservative action/negation vocabulary extends deterministic write discovery for common Tamil, Tanglish and Malay requests. It is not a complete grammar: unsupported or ambiguous phrasing still requires clarification. Exact values come from the current transcript and existing backend checks. Common multilingual date/time tokens feed the existing source-checked reminder validator; missing AM/PM, ranges and contradictory proposed times still fail closed.

Family Shared remains the default. Current private wording in Tamil/Tanglish/Malay is recognized, and group requests route to the authenticated owner's DM. Group transport activation is unchanged; passive Ear capture does not upload audio or execute it. Retrieved/quoted recordings remain evidence, not new commands. Original audio and transcript are retained, and successful current transcript text is made available to existing quote/follow-up readers.

## Configuration and operational limits

After installing 0.5.53 through the normal Home Assistant add-on update screen,
enable voice and configure the key below, then save and restart Alex MCP. Confirm
version 0.5.53 and WhatsApp connectivity in the Web UI before the first recording.

| Setting | Default / meaning |
| --- | --- |
| `voice_notes_enabled` | `false`; explicitly enable for the pilot |
| `voice_openai_api_key` | Masked HA setting; key from the credited API organization. Falls back to the existing OpenAI API key if empty. A dedicated voice key does not change everyday text routing. |
| `voice_monthly_budget_usd` | `5.0`; UTC calendar-month duration estimate. `0` blocks new transcriptions. Separate from text-model budget and actual account balance. |
| `voice_max_seconds` | `180`; configurable 10–600 seconds. Oversized audio is rejected, never silently clipped. |
| `voice_language_hints` | `ta`, `en`, `ms` |
| `voice_keywords` | `Alex`; optional short vocabulary hints, not full private memory |

The API timeout is 25 seconds with automatic SDK retries disabled. Decode has a separate 15-second limit. A durable SQLite reservation precedes each upload. Failed/interrupted requests retain the estimated reservation because the provider may have billed them; they are not automatically replayed after restart. Successful transcripts are cached per original message. Existing inbound/action idempotence still guards household writes.

Silence, invalid audio, missing configuration, budget limits, blank/explicitly unclear transcripts and provider errors keep the original in the pending inbox and return an actionable reply. The endpoint does not supply a confidence score, and Alex does not invent one. Normal-looking transcription errors still require real-recording evaluation.

The usage panel shows the monthly voice reservation separately from text/vision estimates. Health diagnostics include voice attempt count, reserved estimate, total audio duration and average completed latency. These are local operational estimates, not the API credit balance. No credentials or transcripts are included in that summary.

## Where credits can help

| Area | Repo finding | Decision |
| --- | --- | --- |
| Multilingual voice | Input path disabled; premium recognition can address the central requirement | Implement this candidate and evaluate Priya's recordings |
| Difficult interpretation / multi-step requests | Existing brain already has provider fallbacks and a separate semantic router; extra inference can add delay | Compare a stronger model on actual failed requests before enabling another paid route |
| Receipt and bill understanding | Existing local OCR and conditional vision already avoid unnecessary image calls | Consider stronger vision only for measured extraction failures |
| Household memory / taught grocery prices | Persistent scoped storage and teaching already exist | Fix access/routing gaps; credits do not supply missing stored facts or prices |
| Reminders, arithmetic, privacy, duplicate delivery | Deterministic backend responsibilities | Repair code; do not spend model credits as a substitute |

No additional paid reasoning route is enabled by this change. Keep the allowance as available capacity, not a spending target. A stronger model cannot bypass English-only guards, restore missing tool exposure or repair an outbox failure by itself.

Official sources checked on 2 November 2026:

- [File transcription](https://developers.openai.com/api/docs/guides/speech-to-text): `gpt-transcribe`, multilingual hints and recorded-audio input.
- [Model pricing](https://developers.openai.com/api/docs/models/gpt-transcribe): published estimate US$0.0045/minute. Ten minutes daily over 30 days is approximately US$1.35 for transcription alone; existing brain usage is separate.
- [Subscription API credits](https://developers.openai.com/api/docs/guides/chatgpt-api-credits): eligible subscriptions can link API credits to an owned API organization. Current account balance and model access were not inspected in this workspace.

## Acceptance evidence and remaining work

Automated tests use real ffmpeg conversion, SQLite, ingress, MCP services and outbox, with scripted provider responses. They cover native transcript preservation, actual grocery/expense/reminder/memory writes, private handoff, shared group reminders, negative commands, missing AM/PM, duplicate delivery, timeout, silence, malformed input, interrupted upload, and concurrent budget reservations. These establish integration behavior, not ASR accuracy or real model comprehension.

Before claiming the feature works for Priya, test real recordings through the installed WhatsApp path: Tamil, Tanglish and Malay, quiet and everyday background noise, amounts such as RM15/RM50, product/pack distinctions, dates, private instructions, negations, follow-up corrections and compound requests. Record transcript accuracy on critical values, correct persisted action, clarification rate, median/p95 end-to-end latency and actual usage. Wrong private disclosure, a wrong critical value or a duplicated action is a failed pilot case. Compare the current brain and a stronger model only on failures where interpretation is the cause.

This workspace has not connected the credited API key, accessed the running HA installation, or received Priya's recordings. Live recognition, account/model access, production latency and deployment remain unverified. Disable `voice_notes_enabled` to restore the deferred inbox; existing records and audio are retained.

## Local verification result

- Python suite: 942 tests run, 940 passed, 2 existing skips; includes 24 new voice integration tests.
- WhatsApp Node contracts: 4 passed.
- Structural self-test: 5 passed.
- Stress harness and capability catalog: PASS.
- Offline behavior certification: 174 contracts, 1,389 variant checks, 0 failures and 0 new failure signatures.
- Human-AI corpus self-test: 471 packets, no routing gaps or forbidden tool exposure.
- Deterministic reasoning review: 471/471 decisions passed. 465 public packets are unchanged from 0.5.52; six typed teaching packets only gain the updated voice-capable tool description. The exact reviewed delta is stored in `evaluation/multilingual-voice-0553/public-corpus-delta.json`; this is not independent model evidence.
- Final architecture audit: 109 checks passed with the independent review explicitly deferred for the infrastructure reason below. The voice architecture check now covers the current opt-in, durable single-upload path instead of the dormant local Whisper/rescue chain.
- GitHub Actions did not start the release jobs: its annotation reports the account is locked due to a billing issue. The hosted container build and fresh independent model review are therefore unverified; existing workflows are retained.
- Python compilation, new-file lint, JavaScript syntax and whitespace checks passed.
- Real provider requests, Priya recordings, deployed HA behavior and GitHub-hosted independent AI review are not included in these local results.

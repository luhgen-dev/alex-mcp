# Alex 0.5.54 voice and cross-feature audit

The owner reported excellent voice recognition but missing reminder creation,
original receipt delivery and PDF export. Production traces located the failures
after transcription: language-specific authorization/routing and file delivery.
No recognition-model change or additional paid reasoning route is enabled.

## Repairs

- Tamil/Tanglish/Malay reminder intent, colloquial minutes and exact original time evidence.
- Missing-time follow-ups retain the original user words and the correct pending reminder.
- Original receipts, saved pictures, PDFs and home-status cards reach the real outbox.
- Native tool discovery precedes capability-denial answers; recovery is bounded.
- A spoken note is not incorrectly required to save its command recording. An explicitly quoted photo remains the photo.
- Agenda weekday/date and appointment-time grounding; explicit family reminders.
- Scoped unique natural task/plan references; follow-up plan edits do not create another plan.
- Asset-list routing, known Digi-line identity hints and supported monitor enums.

## Verification

- Python: 966 tests, 964 passed, two environment-specific skips.
- Offline behavior: 174 contracts, 1,389 checks, 151 MCP tools, six-tool exposure cap.
- Deterministic reasoning oracle: 471/471 decisions and a reviewed exact corpus fingerprint. This is not an independent AI review.
- Structural self-test: 5/5. Stress harness: 17 invariants. Python compilation passes and pyflakes reports no undefined names. Existing unused-import warnings remain.
- WhatsApp Node contracts: 4/4; bridge implementation was unchanged.
- Real-provider synthetic replay: full cross-domain baseline followed by focused replays of repaired cases. All 230 prompt cases and 26 conversation cases pass in the latest observation after targeted replays; see [live-summary.json](live-summary.json). This is incremental evidence across candidates, not a claim that every case was rerun after every harness-only change.

The live rig uses fresh disposable SQLite databases, a fixed clock, fake HA states,
synthetic transcripts and original-file fixtures. It exercises authenticated voice
ingress, the live provider, actual MCP tools, persisted effects and exact outbox
files. It never runs a WhatsApp sender or controls a physical HA device.

The audit also corrected stale test assumptions: SQLite timestamps, group wake and
pairing provenance, genuine private canaries, valid mutation seed data, direct
selection fast paths, scoped persistent-memory preload, and transport/control state
being mistaken for a household mutation. Expected amounts, due times, privacy,
record identity and exact file delivery assertions remain enforced.

GitHub Actions and its independent review are blocked by the GitHub account billing
lock. Local checks ran with an explicit external-review deferral; workflows and
review requirements were not disabled and a fresh independent review is not claimed.

## Manual acceptance still required

Repeat the owner's actual voice requests in WhatsApp after deployment, confirm
original images/PDFs display, and confirm a short reminder arrives at its due time.
Family @mention/reply behavior, Priya's own recordings, physical HA effects and
current authenticated utility-portal behavior remain real-device/integration gates.
A configured-off utility integration correctly reports unavailable; this audit does
not enable services or promise unsupported SAJ PDF retrieval.

The existing voice configuration and economical everyday provider chain are retained.
The main defects were code/authorization gaps; higher model spending is not needed
to repair them. Audit provider usage figures are estimates, not an account balance.

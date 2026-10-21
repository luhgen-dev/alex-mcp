# Alex learning capability audit

Reviewed 21 October 2026 against baseline commit
`aeb3b74f64510103134a4268ca3e5e98c465646d` (v0.5.43).
This is a source audit of the isolated candidate checkout, not a live household
test or a claim about the currently installed configuration.

## Finding

**Alex remembers records and conversation state, but I found no runtime pipeline
that learns reusable conversational behaviour from a correction or successful
workflow.** Its improvements currently come from engineering changes, tests and
release gates. Shadow-mode observations and transaction corrections provide
useful evidence; they do not themselves revise the interpreter.

“Learning” needs to be separated into four different capabilities:

1. **Remembering data:** saving a fact, a preference or a transaction for later.
2. **Using context:** following a pending question, quoted item or displayed list.
3. **Learning a reusable procedure:** turning a successful interaction or a
   correction into a persistent, validated instruction used in future sessions.
4. **Training the model:** changing model weights.

Alex implements the first two in several explicit forms. The inspected runtime
does not implement the third or fourth. Hermes-style persistent notes and
agent-authored skills would fall mainly into the first and third categories;
they should not be described as model-weight training or guaranteed improvement.

## What exists

| Capability | Evidence at the baseline commit | What it does, and its boundary |
| --- | --- | --- |
| Explicit long-term saved information | `schema.sql`: `saved_items`; `services.py`: `save_item`, `search_saved_items`, `get_saved_item`, `remove_saved_item`; `mcp_server.py`: their exposed tool descriptions | Durable, scoped notes and original image/document links. Search is SQL phrase/word matching. A user can save a preference as a note, but this does not automatically turn it into a global behavioural instruction. |
| Stable household configuration | `profile_config.py`: `normalize_options`, `sync_options`, `authorized_records`, `authorized_records_as_of`, `sync_from_ha`; `init_db.py` calls `profile_config.sync_from_ha` | Income, roster, account aliases, reminder preferences and related facts are versioned from Home Assistant options with ACL checks. The source is explicitly `HA_OPTIONS`; these are configured facts, not preferences inferred from chat. |
| Conversation continuity | `db.py`: `add_turn`, `recent_turns`; `schema.sql`: `conversation_turns`, `pending_items`, `selection_sets`; `brain.py`: `_history_user_text`, `_history_turn_limit`, `respond` | Retains bounded recent conversation and durable pending/selection records. This can support a follow-up without relearning a rule. `recent_turns` returns the last 2–20 rows for the conversation; it is not an unlimited semantic search of past conversations. |
| Exact transaction corrections | `services.py`: `correct_expense`; `schema.sql`: `financial_event_corrections` | Appends a corrected event, supersedes its parent and retains provenance. Fixing an amount changes that record, not the natural-language parser or future extraction policy. |
| Router observations | `shadow_router.py`: `_run`, `record`, `summary`, `ROUTER_INSTRUCTIONS` | Records proposed tools, intent, reference, slots and disagreements; reports observations for review. The inspected path does not promote these observations into new runtime instructions or learned examples. |
| Bounded repair inside a turn | `brain.py`: the `self_repair:capability_denial` and `self_repair:reply_language` branches; provider cooldown handling | Can retry a false capability denial or repair a reply language. It does not persist a successful repair as a reusable skill. Provider cooldowns and `_NO_REASONING_MODELS` in `chatgpt_plan.py` are operational compatibility state, not household understanding. |
| Engineering improvement loop | `ALEX_LAB.md`; `alex_lab.py`; `human_ai_lab.py`; `chatgpt_reasoning_review.py`; `real_ai_snapshot_review.py` | Failure packets, deterministic contracts and model-decision snapshots make repairs reviewable. A developer or engineering assistant still diagnoses, changes code/prompts and reruns gates. The deployed assistant does not automatically rewrite itself from the report. |

Paths in the evidence table are under `alex-mcp/app/` unless stated otherwise.
Function names are used as stable anchors because candidate edits may move line
numbers.

## Evidence that constrains stronger claims

- `brain.py` defines a static `SYSTEM_PROMPT` and composes each model request from
  that prompt, current runtime context, selected recent turns, quote context,
  optional semantic interpretation and the current user input. I found no
  post-conversation skill writer or automatically retrieved correction-example
  store in this path.
- `db.py` seeds and updates `routing_rules` from the source-controlled
  `ROUTING_CATALOGUE`. A table named “rules” is not evidence of rules learned from
  usage.
- `phase2_finance.compare_actual_salary` explicitly returns
  `baseline_changed=False` when observed salary differs from configuration. It
  offers a question rather than silently changing the fixed salary profile.
- `_trace_turn` in `brain.py` describes its rows as local audit evidence, never
  sent to a model or chat, and startup prunes those trace rows after 14 days.
  `shadow_router.record` separately retains router observations for 30 days.
  These are diagnostic retention policies, not a durable learning curriculum.
- Some Phase 2 module headers still say that a component is not wired into the
  live path. The audit therefore reports concrete storage/read functions and
  callers rather than treating a header comment as proof that every option
  affects every live delivery path.

## What the existing lab actually proves

The lab documentation correctly distinguishes a deterministic regression oracle
from an independent model reasoning pass. `real_ai_snapshot_review.py` binds
stored decisions to an exact public-corpus fingerprint and fails when it changes.
Its v3 format also distinguishes unchanged decisions carried from an independent
model review from decisions refreshed by engineering review. Provenance must be
reported per portion of the snapshot.

The candidate adds a v4 snapshot with 59 freshly and independently reviewed
changed packets and 329 byte-identical packets carried forward with their
original, mixed provenance. The fresh decisions disagree with the existing
scorer on ten packets; those answers and the failing gate are preserved. See
`CONVERSATION_CANDIDATE_REPORT.md` for the evidence and interpretation.

`human_ai_lab.score_packets` validates decision kinds, exposed tool capabilities,
unjustified mutators, language and the corpus fingerprint. It does not itself
score exact tool argument values, record IDs, recipient/destination choices or
durable effects. Those require separate checks against the real backend; tool
selection accuracy alone is a narrower result than conversational correctness.

Consequently:

- Passing a mocked semantic response proves that the code handles that response;
  it does not prove that the deployed model will produce it.
- Passing fixed phrase rules proves those regressions remain fixed; it does not
  demonstrate autonomous learning or unseen-language accuracy.
- Re-scoring a frozen model snapshot is not a fresh model run.
- A blind, fresh review of held-out packets provides additional interpretation
  evidence, but still does not measure live provider latency, WhatsApp delivery,
  raw-audio transcription or physical Home Assistant behaviour.

The new `tests/fixtures/conversation_holdout_review.json` is a private expected
outcome specification. Its `expected` and private review metadata must be removed
from any prompt packet supplied to a blind reviewer. Merely adding this file
does not mean its scenarios have been executed.

## Smallest useful next step for learning

First improve and measure the existing interpreter/context path. If recurring
user corrections remain after that work, add a **reviewed correction proposal**
stage: retain the original wording, exact allowed interpretation, context and
owner scope; distinguish a one-off record correction from a general preference;
evaluate it on old and held-out cases; then publish a small versioned approved
example or instruction with a rollback path.

This would be a new capability. It should preserve deterministic identity, ACL,
record selection, time validation, destination routing and idempotency. Automatically
copying every correction into an unrestricted global prompt would mix one-off
facts with behaviour and could transfer one family member's preferences or
private context to another.

No learning feature was implemented during this audit, and no provider, WhatsApp
or Home Assistant action was invoked for it.

## Hermes primary sources

Checked on 21 October 2026:

- [Skills system](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills):
  the agent can create and update reusable procedure documents through
  `skill_manage`, then load them for future work.
- [Persistent memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory):
  bounded persistent notes and user information supply cross-session context.
- [Learn prompt implementation](https://github.com/NousResearch/hermes-agent/blob/main/agent/learn_prompt.py):
  `/learn` gathers the requested source material or procedure and directs the
  agent to create or extend a skill. This changes stored instructions rather
  than model weights.

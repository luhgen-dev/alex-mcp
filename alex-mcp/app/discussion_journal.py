"""Durable Ear journal. Captured evidence is data, never an execution request.

No function in this module creates a Task, Diary, Plan or Reminder. Selection
and mutation APIs recheck the authenticated actor, not a model-supplied scope.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta

import db
import runtime_clock
import scope_policy

STATES = {
    'IDEA': '💭', 'DECISION_PENDING': '❓', 'ACTION_PENDING': '⏳',
    'IN_PROGRESS': '🔄', 'ON_HOLD': '⏸', 'DONE': '✅', 'CANCELLED': '🚫',
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS discussion_journal (
 session_id TEXT PRIMARY KEY REFERENCES discussion_sessions(session_id) ON DELETE CASCADE,
 space_id TEXT NOT NULL, owner_id TEXT NOT NULL REFERENCES users(user_id),
 subject TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'IDEA',
 decisions_json TEXT NOT NULL DEFAULT '[]', unresolved_json TEXT NOT NULL DEFAULT '[]',
 revision INTEGER NOT NULL DEFAULT 1, review_state TEXT NOT NULL DEFAULT 'UNSTRUCTURED', updated_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_discussion_journal_scope ON discussion_journal(space_id,owner_id);
CREATE TABLE IF NOT EXISTS discussion_actions (
 action_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES discussion_sessions(session_id) ON DELETE CASCADE,
 title TEXT NOT NULL, owner_id TEXT REFERENCES users(user_id), due_local TEXT,
 status TEXT NOT NULL, created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
 linked_kind TEXT, linked_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_discussion_actions_session ON discussion_actions(session_id);
CREATE TABLE IF NOT EXISTS discussion_history (
 history_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES discussion_sessions(session_id) ON DELETE CASCADE,
 action_id TEXT, actor_id TEXT NOT NULL, event_kind TEXT NOT NULL,
 before_json TEXT, after_json TEXT NOT NULL, source_message_id TEXT,
 operation_key TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discussion_selection_sets (
 selection_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
 items_json TEXT NOT NULL, read_scope TEXT NOT NULL,
 created_at_utc TEXT NOT NULL, expires_at_utc TEXT NOT NULL
);
"""


def ensure_schema(conn=None):
    own = conn is None
    conn = conn or db.connect()
    try:
        conn.executescript(SCHEMA)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(discussion_journal)")}
        if 'review_state' not in columns:
            conn.execute("ALTER TABLE discussion_journal ADD COLUMN review_state TEXT NOT NULL DEFAULT 'UNSTRUCTURED'")
        # Old captured sessions are already durable; migrate metadata only.
        conn.execute("""INSERT OR IGNORE INTO discussion_journal
            (session_id,space_id,owner_id,subject,updated_at_utc)
            SELECT session_id,'FAMILY_SHARED',started_by_user_id,'',last_message_at_utc
            FROM discussion_sessions""")
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def _now():
    return runtime_clock.now_utc().isoformat()


def _get(conn, actor, session_id):
    row = conn.execute("""SELECT s.*,j.space_id,j.owner_id,j.subject,j.status AS progress_status,
        j.decisions_json,j.unresolved_json,j.revision,j.review_state,j.updated_at_utc
        FROM discussion_sessions s JOIN discussion_journal j USING(session_id)
        WHERE s.session_id=?""", (session_id,)).fetchone()
    spaces = scope_policy.read_spaces(actor)
    if not row or row['space_id'] not in spaces:
        raise PermissionError('discussion is not available in this scope')
    if row['space_id'] != 'FAMILY_SHARED':
        if actor.conversation_type == 'GROUP' or row['owner_id'] != actor.user_id:
            raise PermissionError('private discussion is owner-only')
    elif actor.conversation_type == 'GROUP' and row['conversation_id'] != actor.conversation_id:
        raise PermissionError('discussion belongs to another group')
    return dict(row)


def register(conn, actor, session_id):
    spaces = scope_policy.read_spaces(actor)
    private = actor.conversation_type != 'GROUP' and getattr(actor, 'read_scope', None) == 'private'
    space = actor.private_space if private else 'FAMILY_SHARED'
    if space not in spaces:
        raise PermissionError('discussion scope not available')
    conn.execute("""INSERT OR REPLACE INTO discussion_journal
        (session_id,space_id,owner_id,updated_at_utc) VALUES(?,?,?,?)""",
        (session_id, space, actor.user_id, _now()))


def _history(conn, actor, session_id, kind, after, before=None, action_id=None):
    source = str(getattr(actor, 'action_key', '') or actor.source_message_id or '')
    if not source:
        raise ValueError('discussion changes require a stable source message')
    # Same retried operation has the same key. Distinct operations in a message
    # remain separate; timestamps and incidental model wording are not keys.
    payload = json.dumps([session_id, action_id, kind, after], sort_keys=True)
    key = hashlib.sha256((source + payload).encode()).hexdigest()
    old = conn.execute('SELECT after_json FROM discussion_history WHERE operation_key=?', (key,)).fetchone()
    if old:
        return False
    conn.execute("""INSERT INTO discussion_history VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), session_id, action_id, actor.user_id, kind,
         json.dumps(before) if before is not None else None, json.dumps(after),
         actor.source_message_id, key, _now()))
    return True


def detail(actor, session_id: str) -> dict:
    ensure_schema()
    conn = db.connect()
    try:
        row = _get(conn, actor, session_id)
        messages = [dict(r) for r in conn.execute("""SELECT m.user_id,u.display_name AS participant,
            m.text,m.created_at_utc FROM discussion_messages m JOIN users u USING(user_id)
            WHERE session_id=? ORDER BY m.created_at_utc,m.rowid""", (session_id,))]
        actions = [dict(r) for r in conn.execute("""SELECT a.*,u.display_name AS owner_name
            FROM discussion_actions a LEFT JOIN users u ON a.owner_id=u.user_id
            WHERE session_id=? ORDER BY a.created_at_utc,a.rowid""", (session_id,))]
        history = [dict(r) for r in conn.execute("""SELECT event_kind,action_id,actor_id,before_json,
            after_json,created_at_utc FROM discussion_history WHERE session_id=?
            ORDER BY created_at_utc,rowid""", (session_id,))]
        return {'status': 'found', 'session_id': session_id, 'subject': row['subject'],
                'summary': row['summary_text'], 'progress_status': row['progress_status'],
                'status_icon': STATES[row['progress_status']], 'started_at_utc': row['started_at_utc'],
                'scope': row['space_id'], 'revision': row['revision'], 'review_state': row['review_state'],
                'participants': list(dict.fromkeys(m['participant'] for m in messages)),
                'decisions': json.loads(row['decisions_json']),
                'unresolved': json.loads(row['unresolved_json']), 'actions': actions,
                'messages': messages, 'history': history,
                'presentation_rule': 'Use captured evidence only; distinguish tentative, agreed, rejected and unresolved. Read acknowledgement is not completion. Never expose internal IDs. No Task/Diary/Reminder was created.'}
    finally:
        conn.close()


_STOP_WORDS = set('alex show us our my the a an me what did we discuss discussed discussion discussions session sessions ear most recent latest last recall remember about agree agreed from still pending please tell conversation conversations'.split())


def _tokens(query):
    return [w for w in re.findall(r'[^\W_]+', str(query).casefold()) if len(w) > 1 and w not in _STOP_WORDS]


def list_discussions(actor, query: str = '', pending_only: bool = False, limit: int = 20) -> dict:
    ensure_schema()
    spaces = scope_policy.read_spaces(actor)
    conn = db.connect()
    try:
        rows = conn.execute("""SELECT s.session_id,s.conversation_id,s.summary_text,s.started_at_utc,
             j.* FROM discussion_sessions s JOIN discussion_journal j USING(session_id)
             WHERE s.status IN ('STOPPED','TIMED_OUT') ORDER BY COALESCE(s.ended_at_utc,s.last_message_at_utc) DESC,s.rowid DESC""").fetchall()
        tokens = _tokens(query)
        matches = []
        for row in rows:
            if row['space_id'] not in spaces:
                continue
            try:
                _get(conn, actor, row['session_id'])
            except PermissionError:
                continue
            texts = conn.execute('SELECT text FROM discussion_messages WHERE session_id=?', (row['session_id'],)).fetchall()
            hay = ' '.join([row['subject'], row['summary_text'] or '', row['decisions_json']] + [r['text'] or '' for r in texts]).casefold()
            if tokens and not all(t in hay for t in tokens):
                continue
            actions = [dict(a) for a in conn.execute("""SELECT a.title,a.owner_id,a.status,a.due_local,u.display_name AS owner_name FROM discussion_actions a LEFT JOIN users u ON a.owner_id=u.user_id
                WHERE session_id=? ORDER BY a.created_at_utc,a.rowid""", (row['session_id'],))]
            if pending_only:
                actions = [a for a in actions if a['status'] not in ('DONE', 'CANCELLED') and a['owner_id'] in (None, actor.user_id)]
                if not actions and not json.loads(row['unresolved_json'] or '[]'):
                    continue
            matches.append({'number': len(matches) + 1, 'session_id': row['session_id'],
                'subject': row['subject'] or next((r['text'][:100] for r in texts if r['text']), 'Discussion'), 'date': row['started_at_utc'],
                'participants': [r[0] for r in conn.execute('SELECT DISTINCT u.display_name FROM discussion_messages m JOIN users u USING(user_id) WHERE session_id=?', (row['session_id'],))],
                'summary': row['summary_text'] or '', 'progress_status': row['status'],
                'status_icon': STATES[row['status']], 'actions': actions})
            if len(matches) >= max(1, min(50, int(limit))):
                break
        sid = str(uuid.uuid4())
        now = runtime_clock.now_utc()
        conn.execute('INSERT INTO discussion_selection_sets VALUES(?,?,?,?,?,?,?)',
            (sid, actor.user_id, actor.conversation_id, json.dumps([m['session_id'] for m in matches]),
             getattr(actor, 'read_scope', None) or 'family', now.isoformat(), (now + timedelta(minutes=30)).isoformat()))
        conn.commit()
        return {'status': 'found' if matches else 'not_found', 'discussions': matches,
                'selection_id': sid, 'selection_kind': 'DISCUSSION',
                'presentation_rule': 'Show this exact numbered discussion list. Numbers bind to this list only; never substitute saved items.'}
    finally:
        conn.close()


def resolve_choice(actor, choice: int, selection_id: str | None = None) -> dict:
    ensure_schema()
    conn = db.connect()
    try:
        sql = 'SELECT * FROM discussion_selection_sets WHERE user_id=? AND conversation_id=? AND expires_at_utc>?'
        args = [actor.user_id, actor.conversation_id, _now()]
        if selection_id:
            sql += ' AND selection_id=?'
            args.append(selection_id)
        row = conn.execute(sql + ' ORDER BY created_at_utc DESC,rowid DESC LIMIT 1', args).fetchone()
        ids = json.loads(row['items_json']) if row else []
        if int(choice) < 1 or int(choice) > len(ids):
            raise ValueError('choose a number from the current discussion list')
        target = ids[int(choice) - 1]
    finally:
        conn.close()
    return detail(actor, target)


def update_discussion(actor, session_id: str, *, subject: str | None = None,
                      status: str | None = None, decisions: list[str] | None = None,
                      unresolved: list[str] | None = None, expected_revision: int | None = None) -> dict:
    ensure_schema()
    if status is not None and status not in STATES:
        raise ValueError('unsupported discussion status; acknowledgement is not completion')
    conn = db.connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        old = _get(conn, actor, session_id)
        changes = {k: v for k, v in {'subject': subject, 'status': status,
            'decisions_json': json.dumps(decisions) if decisions is not None else None,
            'unresolved_json': json.dumps(unresolved) if unresolved is not None else None}.items() if v is not None}
        if not changes:
            return {'status': 'unchanged', 'session_id': session_id}
        for value in (decisions, unresolved):
            if value is not None and (not isinstance(value, list) or any(not isinstance(v, str) for v in value)):
                raise ValueError('discussion evidence must be a list of text entries')
        if _history(conn, actor, session_id, 'DISCUSSION_UPDATED', changes, old):
            if expected_revision is not None and old['revision'] != expected_revision:
                raise ValueError('discussion changed; read its latest state before updating')
            conn.execute('UPDATE discussion_journal SET ' + ','.join(k + '=?' for k in changes) + ',revision=revision+1,updated_at_utc=? WHERE session_id=?',
                [*changes.values(), _now(), session_id])
        conn.commit()
    finally:
        conn.close()
    return detail(actor, session_id)


def update_action(actor, session_id: str, title: str | None = None, action_id: str | None = None,
                  status: str | None = None, owner_id: str | None = None,
                  due_local: str | None = None, clear_due: bool = False) -> dict:
    """Add an explicitly requested discussion action or update an exact action.

    These are journal entries, not task/reminder scheduling. The caller must
    clarify competing actions rather than fabricate an identifier or owner.
    """
    ensure_schema()
    if status is not None and status not in STATES:
        raise ValueError('unsupported action status; read/acknowledged is not done')
    if due_local is not None:
        due_local = _grounded_due(due_local, [{
            'text': getattr(actor, 'trusted_text', ''),
            'created_at_utc': getattr(actor, 'received_at_utc', '') or _now(),
        }], actor)
    conn = db.connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        session = _get(conn, actor, session_id)
        if owner_id is not None:
            if session['space_id'] != 'FAMILY_SHARED' and owner_id != actor.user_id:
                raise PermissionError('private actions cannot be assigned to another owner')
            if not conn.execute('SELECT 1 FROM users WHERE user_id=?', (owner_id,)).fetchone():
                raise ValueError('unknown action owner; ask which participant')
        old = None
        if action_id:
            row = conn.execute('SELECT * FROM discussion_actions WHERE session_id=? AND action_id=?', (session_id, action_id)).fetchone()
            if not row:
                raise ValueError('action is not in this discussion')
            old = dict(row)
        elif not str(title or '').strip():
            raise ValueError('new discussion action needs a title')
        effective_status = status if status is not None else (old['status'] if old else 'ACTION_PENDING')
        values = {'title': str(title).strip() if title is not None else old['title'],
                  'status': effective_status, 'owner_id': owner_id if owner_id is not None else (old or {}).get('owner_id'),
                  'due_local': None if clear_due else due_local if due_local is not None else (old or {}).get('due_local')}
        # Stable creation identity prevents duplicate actions on delivery retry.
        if action_id is None:
            seed = str(actor.action_key or actor.source_message_id) + session_id + values['title']
            action_id = str(uuid.uuid5(uuid.NAMESPACE_URL, seed))
        if _history(conn, actor, session_id, 'ACTION_UPDATED' if old else 'ACTION_ADDED', values, old, action_id):
            conn.execute("""INSERT INTO discussion_actions(action_id,session_id,title,owner_id,due_local,status,created_at_utc,updated_at_utc)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(action_id) DO UPDATE SET title=excluded.title,owner_id=excluded.owner_id,
                due_local=excluded.due_local,status=excluded.status,updated_at_utc=excluded.updated_at_utc""",
                (action_id, session_id, values['title'], values['owner_id'], values['due_local'], effective_status, _now(), _now()))
        conn.commit()
    finally:
        conn.close()
    result = detail(actor, session_id)
    result['updated_action_id'] = action_id
    return result


def link_action(actor, session_id: str, action_id: str, target_kind: str,
                target_id: str, confirmed: bool = False) -> dict:
    """Record an explicitly authorized promotion, never create its target.

    The target must already exist in the SAME scope. A Family discussion cannot
    disclose a private task, and private discussion provenance cannot be linked
    into a shared record. Existing links are immutable (history is preserved).
    """
    if confirmed is not True:
        return {'status': 'needs_confirmation', 'nothing_written': True,
                'clarification_question': 'Should I link this discussion action to that existing record?'}
    kinds = {'TASK': ('tasks', 'task_id'), 'DIARY': ('diary_events', 'diary_id'),
             'PLAN': ('plans', 'plan_id'), 'REMINDER': ('reminders', 'reminder_id')}
    if target_kind not in kinds:
        raise ValueError('unsupported discussion action link')
    ensure_schema()
    conn = db.connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        session = _get(conn, actor, session_id)
        action = conn.execute('SELECT * FROM discussion_actions WHERE session_id=? AND action_id=?',
                              (session_id, action_id)).fetchone()
        if not action:
            raise ValueError('action is not in this discussion')
        table, column = kinds[target_kind]
        target = conn.execute(f'SELECT space_id FROM {table} WHERE {column}=?', (target_id,)).fetchone()
        if not target or target['space_id'] != session['space_id']:
            raise PermissionError('linked record must exist in the same authorized scope')
        if action['linked_id'] and (action['linked_id'], action['linked_kind']) != (target_id, target_kind):
            raise ValueError('discussion action already has a stable link')
        values = {'linked_kind': target_kind, 'linked_id': target_id}
        if _history(conn, actor, session_id, 'ACTION_LINKED', values, dict(action), action_id):
            conn.execute('UPDATE discussion_actions SET linked_kind=?,linked_id=?,updated_at_utc=? WHERE action_id=?',
                         (target_kind, target_id, _now(), action_id))
        conn.commit()
    finally:
        conn.close()
    return detail(actor, session_id)


def _grounded_due(value, evidence, actor):
    """Accept explicit calendar dates or a single grounded weekday, never a guess."""
    if value is None:
        return None
    from zoneinfo import ZoneInfo
    proposed = datetime.fromisoformat(str(value)).date()
    from reminder_time import dates_in, canonical_date
    weekdays = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
    for row in evidence:
        text = str(row['text'] or '').casefold().replace('’', "'")
        # Ambiguous/negated dates are not commitments. A summarizer must keep
        # them unresolved (null due date) instead of choosing a plausible day.
        if re.search(r"\b(?:next|maybe|perhaps|tentative|might|could)\b|"
                     r"\b(?:haven't|have not|hasn't|has not|not yet|not)\s+(?:yet\s+)?(?:decided|agreed|chosen|set|sure)\b|"
                     r"\bno\s+(?:date|deadline)\b|\bnot\s+(?:on\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|tomorrow)\b",
                     text):
            continue
        for candidate in dates_in(text):
            try:
                canonical = canonical_date(candidate)
                try:
                    explicit = datetime.fromisoformat(canonical).date()
                except ValueError:
                    explicit = datetime.strptime(canonical, '%d %B %Y').date()
                if explicit == proposed:
                    return proposed.isoformat()
            except (ValueError, TypeError):
                pass
        if re.search(r'\b(today|tomorrow)\b', text):
            anchor = datetime.fromisoformat(row['created_at_utc'].replace('Z', '+00:00'))
            if anchor.tzinfo is None:
                from datetime import timezone
                anchor = anchor.replace(tzinfo=timezone.utc)
            base = anchor.astimezone(ZoneInfo(actor.timezone)).date()
            relative = {'today': base, 'tomorrow': base + timedelta(days=1)}
            candidates = {relative[w] for w in relative if re.search(r'\b' + w + r'\b', text)}
            if len(candidates) == 1 and proposed in candidates:
                return proposed.isoformat()
        names = [name for name in weekdays if re.search(r'\b' + name + r'\b', text)]
        if len(names) == 1:
            anchor = datetime.fromisoformat(row['created_at_utc'].replace('Z', '+00:00'))
            if anchor.tzinfo is None:
                from datetime import timezone
                anchor = anchor.replace(tzinfo=timezone.utc)
            base = anchor.astimezone(ZoneInfo(actor.timezone)).date()
            if base + timedelta(days=(weekdays.index(names[0]) - base.weekday()) % 7) == proposed:
                return proposed.isoformat()
    raise ValueError('discussion due date is not grounded in captured evidence')


def accept_structured_summary(actor, session_id: str, payload: dict) -> dict:
    """Validate and atomically store the stop-summary interpretation.

    Evidence references must be original messages in this exact session. This
    does not grant captured text execution authority or promote any action.
    Failed validation leaves the already-persisted unstructured capture intact.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get('summary'), str):
        raise ValueError('structured discussion summary required')
    ensure_schema()
    conn = db.connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        session = _get(conn, actor, session_id)
        if session['status'] not in ('STOPPED', 'TIMED_OUT'):
            raise ValueError('stop listening before summarizing')
        source = {r['message_id']: dict(r) for r in conn.execute(
            'SELECT * FROM discussion_messages WHERE session_id=?', (session_id,))}
        participants = {r['user_id'] for r in source.values()}
        def evidence(item):
            if not isinstance(item, dict):
                raise ValueError('summary entries require evidence references')
            refs = item.get('evidence_message_ids')
            if not isinstance(refs, list) or not refs or any(not isinstance(v, str) or v not in source for v in refs):
                raise ValueError('summary evidence is not in this discussion')
            return [source[v] for v in refs]
        decisions, unresolved, actions = [], [], []
        for key, destination in (('decisions', decisions), ('unresolved', unresolved)):
            entries = payload.get(key, [])
            if not isinstance(entries, list) or len(entries) > 100:
                raise ValueError('bounded discussion entries required')
            for item in entries:
                evidence(item)
                text = item.get('text')
                if not isinstance(text, str) or not text.strip():
                    raise ValueError('summary entry text required')
                destination.append(text[:2000])
        entries = payload.get('actions', [])
        if not isinstance(entries, list) or len(entries) > 100:
            raise ValueError('bounded discussion actions required')
        for item in entries:
            sources = evidence(item)
            if item.get('status') not in STATES or not isinstance(item.get('title'), str) or not item['title'].strip():
                raise ValueError('discussion action needs title and explicit valid state')
            owner = item.get('owner_id')
            if owner is not None and owner not in participants:
                raise ValueError('action owner must be a known captured participant')
            if owner is not None and session['space_id'] != 'FAMILY_SHARED' and owner != actor.user_id:
                raise PermissionError('private action owner must be this owner')
            actions.append({'title': item['title'][:2000], 'status': item['status'],
                'owner_id': owner, 'due_local': _grounded_due(item.get('due_local'), sources, actor),
                'evidence_message_ids': item['evidence_message_ids']})
        validated = {'summary': payload['summary'][:20000], 'subject': str(payload.get('subject') or '')[:200],
                     'decisions': decisions, 'unresolved': unresolved, 'actions': actions}
        # A stopped-session summary is a one-time ingestion. Later discussion
        # changes use the revisioned APIs and cannot be undone by a stop retry.
        existing = conn.execute("SELECT 1 FROM discussion_history WHERE session_id=? AND event_kind='CAPTURE_STRUCTURED'", (session_id,)).fetchone()
        if not existing and _history(conn, actor, session_id, 'CAPTURE_STRUCTURED', validated):
            conn.execute('UPDATE discussion_sessions SET summary_text=? WHERE session_id=?',
                         (validated['summary'], session_id))
            status = 'DECISION_PENDING' if unresolved else 'ACTION_PENDING' if actions or decisions else 'IDEA'
            conn.execute("""UPDATE discussion_journal SET subject=?,status=?,decisions_json=?,unresolved_json=?,
                review_state='STRUCTURED',revision=revision+1,updated_at_utc=? WHERE session_id=?""",
                (validated['subject'], status, json.dumps(decisions), json.dumps(unresolved), _now(), session_id))
            for index, action in enumerate(actions):
                aid = str(uuid.uuid5(uuid.NAMESPACE_URL, session_id + ':capture:' + str(index)))
                conn.execute("""INSERT INTO discussion_actions(action_id,session_id,title,owner_id,due_local,status,created_at_utc,updated_at_utc)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (aid, session_id, action['title'], action['owner_id'], action['due_local'], action['status'], _now(), _now()))
        conn.commit()
    finally:
        conn.close()
    return detail(actor, session_id)

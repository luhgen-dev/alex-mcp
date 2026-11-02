"""Chat-taught household knowledge and reusable, explicitly labelled estimates.

The saved-item index owns visibility and deletion. Structured metadata adds
deterministic price reuse; version events make updates idempotent and inspectable.
Recall supplies data, never tool permissions or instructions to execute actions.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

import db
import runtime_clock
import scope_policy
from text_normalization import normalize_intent_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS learned_memory (
 item_id TEXT PRIMARY KEY REFERENCES saved_items(item_id) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('fact','preference','price')),
 subject_key TEXT NOT NULL, topics_json TEXT NOT NULL,
 payload_json TEXT NOT NULL, version INTEGER NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learned_memory_events (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT,
 item_id TEXT NOT NULL REFERENCES saved_items(item_id) ON DELETE CASCADE,
 action_key TEXT NOT NULL UNIQUE, version INTEGER NOT NULL,
 snapshot_json TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_learned_subject ON learned_memory(kind,subject_key);
"""

_STOP = set('a an the my our your we i you me us is are was be it this that to for of '
            'in on at from and or with what which how much about do does have price prices '
            'cost costs check show tell please alex remember saved estimate estimated '
            'approximately roughly usual normally use buy buying need'.split())
_WRITE = re.compile(r"\b(?:remember|save|teach|update|change|correct|learn)\b", re.I)
RECALL_REQUEST = re.compile(r'\b(?:taught|learned|learnt|preferences|price history|remembered prices)\b', re.I)


def words(text):
    normalized = normalize_intent_text(str(text or '')).casefold()
    normalized = re.sub(r'(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])', ' ', normalized)
    tokens = re.findall(r'[^\W_]+', normalized)
    singular = {'diapers': 'diaper', 'packs': 'pack', 'wipes': 'wipe', 'prices': 'price'}
    return {singular.get(t, t) for t in tokens if t not in _STOP}


def key(text):
    return ' '.join(re.findall(r'[^\W_]+', str(text).casefold()))


def _clean(value, limit, label):
    value = scope_policy.strip_control_emoji(str(value or '')).strip()
    if not value or len(value) > limit:
        raise ValueError(f'{label} must contain 1–{limit} characters.')
    return value


def teaching_tools(text):
    """Narrow explicit teaching surface; ordinary saved-note flows stay intact."""
    text = normalize_intent_text(text)
    if not _WRITE.search(text):
        return set()
    explicit = re.search(r'\b(?:remember|teach|learn)\b', text, re.I)
    if not explicit and re.search(r'\bupdate\s+me\b', text, re.I):
        return set()
    teaching_subject = re.search(r'\b(?:reference|estimate|remembered|preference|preferences|usual|price|prices|memory)\b', text, re.I)
    if not explicit and not teaching_subject:
        return set()
    if re.search(r'\b(?:bill|bills|payment|expense|spent|paid|salary|income|tnb|saj|digi)\b', text, re.I):
        return set()
    if re.search(r'\b(?:price|prices|costs?|rm\s*\d+|myr\s*\d+)\b', text, re.I):
        return {'remember_product_price', 'remember_knowledge', 'recall_knowledge'}
    if re.search(r'\b(?:preference|prefer|usual|usually|always|from now on|teach|learn|fact|memory|remembered)\b', text, re.I):
        return {'remember_knowledge', 'recall_knowledge'}
    return set()


def _authorize(actor):
    from brain import _trusted_mutation_requested
    if (not actor.action_key or actor.source == 'voice' or
            not _WRITE.search(actor.trusted_text) or
            not _trusted_mutation_requested(actor.trusted_text)):
        raise ValueError('Please explicitly ask me to remember or update this in a typed message.')
    space = scope_policy.resolve_new_write_space(actor)
    if space not in actor.allowed_spaces:
        raise PermissionError('That memory scope is not accessible.')
    return space


def _save(actor, kind, subject, content, topics, payload, identity):
    space = _authorize(actor)
    if kind == 'preference' and 'price_fallback' not in payload:
        identity = actor.user_id + '|' + identity
    stamp = db.utc_now()
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        replay = conn.execute('SELECT snapshot_json FROM learned_memory_events WHERE action_key=?',
                              (actor.action_key,)).fetchone()
        if replay:
            return {**json.loads(replay[0]), 'status': 'already_saved'}
        old = conn.execute('''SELECT s.item_id,l.version FROM learned_memory l
            JOIN saved_items s USING(item_id) WHERE s.space_id=? AND l.kind=? AND l.subject_key=?''',
                           (space, kind, identity)).fetchone()
        item_id, version = (old['item_id'], old['version'] + 1) if old else (str(uuid.uuid4()), 1)
        if old:
            conn.execute('UPDATE saved_items SET title=?,content=?,tags=? WHERE item_id=?',
                         (subject, content, ','.join(topics), item_id))
        else:
            conn.execute('''INSERT INTO saved_items(item_id,action_key,source_message_id,space_id,
                owner_id,title,content,tags,created_at_utc) VALUES(?,?,?,?,?,?,?,?,?)''',
                         (item_id, actor.action_key, actor.source_message_id, space, actor.user_id,
                          subject, content, ','.join(topics), stamp))
        conn.execute('''INSERT INTO learned_memory VALUES(?,?,?,?,?,?,?) ON CONFLICT(item_id)
            DO UPDATE SET topics_json=excluded.topics_json,payload_json=excluded.payload_json,
            version=excluded.version,updated_at=excluded.updated_at''',
                     (item_id, kind, identity, json.dumps(topics), json.dumps(payload), version, stamp))
        result = {'status': 'updated' if old else 'saved', 'item_id': item_id,
                  'kind': kind, 'subject': subject, 'content': content, 'scope': space,
                  'version': version, 'recorded_at': stamp, 'data': payload}
        conn.execute('''INSERT INTO learned_memory_events(item_id,action_key,version,snapshot_json,
            recorded_at) VALUES(?,?,?,?,?)''', (item_id, actor.action_key, version, json.dumps(result), stamp))
        if kind == 'price' or 'price_fallback' in payload:
            # Existing open items should pick up the correction silently. Completed
            # purchases retain their historical snapshots; no expense is created.
            conn.execute('''UPDATE grocery_price_jobs SET state='QUEUED',attempts=0,next_try=?,lease_until=NULL
                WHERE item_id IN (SELECT item_id FROM shopping_items WHERE space_id=? AND status='OPEN')''',
                         (stamp, space))
        return result


def remember(actor, subject, content, kind='fact', topics=None, price_fallback=None):
    if kind not in {'fact', 'preference'}:
        raise ValueError('Choose fact or preference; use remember_product_price for a reusable price.')
    subject = _clean(subject, 160, 'Subject')
    content = _clean(content, 1500, 'Knowledge')
    topics = [_clean(t, 50, 'Topic') for t in (topics or [])[:8]]
    if kind == 'preference' and not topics:
        raise ValueError('Specify when this preference applies, such as groceries, meals or replies.')
    payload = {}
    if price_fallback is not None:
        if kind != 'preference' or not re.search(r'price|grocer|estimate|online', actor.trusted_text, re.I):
            raise ValueError('Price fallback must be an explicitly taught grocery pricing preference.')
        payload['price_fallback'] = bool(price_fallback)
        # A single setting key prevents two differently-worded policy notes
        # silently competing with each other.
        subject = 'Grocery price fallback'
        topics = list(dict.fromkeys(['groceries', 'prices', *topics]))
    return _save(actor, kind, subject, content, topics, payload, key(subject))


def remember_price(actor, product, pack_description, unit_price, retailer=None, aliases=None, observed_on=None):
    product = _clean(product, 140, 'Product')
    pack = _clean(pack_description, 100, 'Pack/size')
    store = _clean(retailer, 80, 'Retailer') if retailer else None
    try:
        amount = Decimal(str(unit_price))
        if not amount.is_finite() or not 0 < amount <= 100000:
            raise ValueError('Use a positive MYR price per pack.')
        amount = amount.quantize(Decimal('.01'))
    except InvalidOperation as exc:
        raise ValueError('Use a numeric MYR price per pack.') from exc
    # A standalone model guess cannot become a permanent price. Receipt evidence
    # is allowed only when the current typed caption explicitly asks to learn it.
    amounts = re.findall(r'(?i)\b(?:rm|myr)\s*(\d+(?:\.\d{1,2})?)(?![\w.])', actor.trusted_text)
    if not amounts:
        if re.search(r'(?i)\b(?:sgd|usd|eur|gbp)\b|[sS]\$', actor.trusted_text):
            raise ValueError('This price reference uses MYR. Please provide the MYR amount; no currency conversion is assumed.')
        amounts = re.findall(r'(?<!\w)(\d+(?:\.\d{1,2})?)(?![\w.])', actor.trusted_text)
    if not actor.media_ids and not any(Decimal(x) == amount for x in amounts):
        raise ValueError('Please state the price you want me to remember, or send its receipt with that instruction.')
    aliases = [_clean(a, 100, 'Alias') for a in (aliases or [])[:8]]
    observed = date.fromisoformat(observed_on) if observed_on else runtime_clock.today(actor.timezone)
    if observed > runtime_clock.today(actor.timezone):
        raise ValueError('A reference price cannot have a future observation date.')
    payload = {'product': product, 'pack_description': pack, 'unit_price': f'{amount:.2f}',
               'retailer': store, 'aliases': aliases, 'observed_on': observed.isoformat(),
               'currency': 'MYR', 'source_kind': 'user_reference', 'estimate': True}
    title = product + ' — ' + pack
    content = f"{title}: approximately RM{amount:.2f} per pack" + (f' at {store}' if store else '')
    content += f". Reference dated {observed.isoformat()}; not a live quote or expense."
    return _save(actor, 'price', title, content, ['groceries', product, *aliases], payload,
                 key(product) + '|' + key(pack) + '|' + key(store or ''))


def _rows(conn, spaces):
    marks = ','.join('?' for _ in spaces)
    return conn.execute(f'''SELECT s.item_id,s.title,s.content,s.tags,s.space_id,s.owner_id,
        s.created_at_utc,l.kind,l.topics_json,l.payload_json,l.version,l.updated_at
        FROM saved_items s LEFT JOIN learned_memory l USING(item_id)
        WHERE s.space_id IN ({marks}) AND s.media_id IS NULL
        ORDER BY COALESCE(l.updated_at,s.created_at_utc) DESC,s.rowid DESC''', spaces).fetchall()


def _public(row, history=None):
    result = {'item_id': row['item_id'], 'kind': row['kind'] or 'saved_note',
              'subject': row['title'], 'content': row['content'], 'scope': row['space_id'],
              'taught_by': row['owner_id'],
              'recorded_at': row['updated_at'] or row['created_at_utc'], 'version': row['version'] or 1}
    if row['payload_json']:
        result['data'] = json.loads(row['payload_json'])
    if history is not None:
        result['history'] = history
    return result


def recall(actor, query=None, include_history=False, limit=10):
    terms = words(query)
    with db.connect() as conn:
        matches = []
        for row in _rows(conn, scope_policy.read_spaces(actor)):
            subject = words(row['title'] + ' ' + (row['tags'] or ''))
            content = words(row['content'])
            overlap = terms & (subject | content)
            if terms and not overlap:
                continue
            score = len(terms & subject) * 3 + len(overlap)
            matches.append((score, row))
        matches.sort(key=lambda x: x[0], reverse=True)
        result = []
        for _, row in matches[:max(1, min(20, limit))]:
            history = None
            if include_history:
                history = [json.loads(e[0]) for e in conn.execute('''SELECT snapshot_json FROM
                    learned_memory_events WHERE item_id=? ORDER BY version DESC LIMIT 20''', (row['item_id'],))]
            result.append(_public(row, history))
        return {'memories': result, 'note': 'Saved household data; current instructions and access rules take priority.'}


def context_for_turn(actor, text):
    """Bounded automatic recall, re-scoped on EVERY turn, independent of chat history."""
    terms = words(text)
    if not terms:
        return []
    with db.connect() as conn:
        ranked = []
        for row in _rows(conn, scope_policy.read_spaces(actor)):
            if row['kind'] == 'price':
                continue  # Price evidence is resolved by the deterministic path.
            # Shared visibility doesn't make one person's reply/style preference
            # another person's preference. Structured grocery policy is shared
            # with the scoped shopping list and applied deterministically.
            if row['kind'] == 'preference' and row['owner_id'] != actor.user_id:
                continue
            subject = words(row['title'] + ' ' + (row['tags'] or ''))
            topics = {key(t) for t in json.loads(row['topics_json'] or '[]')}
            general = row['kind'] == 'preference' and bool(topics & {'general', 'replies', 'presentation'})
            overlap = terms & subject
            if not overlap and not general:
                continue
            ranked.append((len(overlap), row))
        ranked.sort(key=lambda x: x[0], reverse=True)
        return [{**_public(row), 'content': row['content'][:1500]} for _, row in ranked[:6]]


def reference(spaces, query, quantity=1, retailer=None):
    """Only complete token matches or explicitly taught aliases; ambiguity stays visible."""
    wanted = words(query)
    if not wanted or not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= 10000:
        return None
    candidates = []
    with db.connect() as conn:
        rows = _rows(conn, spaces)
        if any(json.loads(r['payload_json'] or '{}').get('price_fallback') is False
               for r in rows if r['kind'] == 'preference'):
            return None
        for row in rows:
            if row['kind'] != 'price':
                continue
            data = json.loads(row['payload_json'])
            if retailer and key(data.get('retailer') or '') != key(retailer):
                continue
            names = [data['product'] + ' ' + data['pack_description'], *data['aliases']]
            if not any(wanted <= words(n) for n in names):
                continue
            candidates.append((row, data))
    if len(candidates) != 1:
        return {'status': 'needs_choice', 'note': 'More than one remembered pack matches.',
                'candidates': [{'title': r['title'], 'retailer': d['retailer'], 'unit_price': d['unit_price']}
                               for r, d in candidates]} if candidates else None
    row, data = candidates[0]
    return {'status': 'priced', 'title': row['title'], 'unit_price': data['unit_price'],
            'line_total': f"{Decimal(data['unit_price']) * quantity:.2f}", 'packs': quantity,
            'retailer': data['retailer'], 'currency': 'MYR', 'estimate': True,
            'source_kind': 'learned_reference', 'learned_item_id': row['item_id'],
            'learned_version': row['version'], 'observed_on': data['observed_on'],
            'checked_at': row['updated_at'], 'note': 'Remembered budget estimate, not a live store price or purchase.'}


def valid_reference(conn, quote, space):
    if quote.get('source_kind') != 'learned_reference':
        return True
    if any(json.loads(r['payload_json'] or '{}').get('price_fallback') is False
           for r in _rows(conn, [space]) if r['kind'] == 'preference'):
        return False
    row = conn.execute('''SELECT l.version FROM learned_memory l JOIN saved_items s USING(item_id)
        WHERE l.item_id=? AND s.space_id=?''', (quote.get('learned_item_id'), space)).fetchone()
    return bool(row and row[0] == quote.get('learned_version'))


def price_fallback(actor, query, quantity=1, retailer=None):
    return reference(scope_policy.read_spaces(actor), query, quantity, retailer)


def render_reference(quote):
    if not quote:
        return ''
    if quote['status'] != 'priced':
        return 'Remembered prices: more than one pack matches. Please specify the size/pack.'
    store = ' at ' + quote['retailer'] if quote.get('retailer') else ''
    return (f"Remembered estimate — {quote['title']}: approximately RM {quote['unit_price']} per pack{store}.\n"
            f"Reference dated {quote['observed_on']}. Not a live store quote.")


def augment_catalogue(actor, payload, query, user_text):
    """Private scoped fallback stays local: no household memory goes to a retailer."""
    if payload.get('products'):
        return payload
    # Explicit current-only requests are honoured even if the stored default
    # allows an estimate. Historical memory remains available via recall.
    if re.search(r'\b(?:live|current|today|online)\b.*\bonly\b|\bonly\b.*\b(?:live|current|online)\b', user_text, re.I):
        return payload
    stores = re.findall(r'\b(?:mydin|manjaku)\b', user_text, re.I)
    quote = price_fallback(actor, query, retailer=stores[0] if len(set(s.lower() for s in stores)) == 1 else None)
    return {**payload, '_learned_reference': quote} if quote else payload

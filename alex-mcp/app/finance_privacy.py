"""Owner-authorized visibility changes of a durable quoted expense.

The destination's read scope cannot identify the shared source. This operation
uses the authenticated inbound quote instead; it never widens general reads.
"""
import json
import re
import threading
import fcntl
import os
from contextlib import contextmanager

import db
import runtime_clock
import scope_policy
from config import normalize_phone
from text_normalization import normalize_intent_text


class PrivacyDeliveryError(ValueError):
    pass


# Ingress and scheduler are separate services. Serialize visibility commits
# with final egress checks/sends across both threads and processes.
_delivery_threads = threading.RLock()
EXPIRED_NOTICE = 'This result is out of date. Please ask again for a fresh result.'


@contextmanager
def delivery_guard():
    with _delivery_threads:
        fd = os.open(str(db.DB_PATH) + '.privacy-delivery.lock',
                     os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def requested(text):
    value = normalize_intent_text(text).casefold()
    if not scope_policy.explicit_private(value):
        return False
    if re.search(r"\b(?:don't|dont|do not|not|never|without|hypothetical|how|if)\b", value):
        return False
    return bool(re.search(r"\b(?:move|save|make|put|keep|change|switch|set)\b", value)
                and re.search(r"\b(?:that|this|it|expense|transaction|record)\b", value))


def _own_destination(conn, owner_id, destination):
    if not destination or str(destination).endswith('@g.us'):
        return False
    phones = conn.execute('''SELECT phone_number FROM user_phone_history
        WHERE user_id=? AND valid_to_utc IS NULL''', (owner_id,)).fetchall()
    for phone in phones:
        normalized = normalize_phone(phone[0])
        if destination == normalized.lstrip('+') + '@s.whatsapp.net':
            return True
        if str(destination).endswith('@lid') and conn.execute('''SELECT 1 FROM inbound_messages
            WHERE conversation_id=? AND sender_phone=? AND conversation_type='DIRECT_DM' LIMIT 1''',
                (destination, normalized)).fetchone():
            return True
    return False


def move_quoted_expense(actor):
    """Move one exact owned correction chain, keeping IDs, amounts and files.

    Called only for an explicit current-turn request. A model cannot supply the
    event ID or quote: both are recovered from locally persisted provenance.
    """
    if not requested(actor.trusted_text):
        raise PermissionError('An explicit request to make this expense private is required.')
    if actor.conversation_type != 'DIRECT_DM' or actor.private_handoff:
        raise PermissionError('Reply to the expense confirmation in your own DM to make it private.')
    if actor.private_space not in actor.allowed_spaces:
        raise PermissionError('Your private space is unavailable.')
    with delivery_guard(), db.connect() as conn:
        inbound = conn.execute('SELECT * FROM inbound_messages WHERE message_id=?',
                               (actor.source_message_id,)).fetchone()
        if (not inbound or inbound['conversation_id'] != actor.conversation_id
                or normalize_phone(inbound['sender_phone']) != actor.phone):
            raise PermissionError('The current message could not be authenticated.')
        quote = db.resolve_quoted_context(actor.conversation_id, inbound['quoted_message_id'], actor.phone)
        if not quote:
            raise ValueError('Reply to the exact expense confirmation to choose which record to move.')
        event_id = (quote.get('context_id') if quote.get('context_kind') == 'FINANCIAL_EVENT'
                    else (quote.get('financial_event') or {}).get('event_id'))
        if not event_id:
            raise ValueError('That reply does not identify one expense. Reply to its individual confirmation.')
        conn.execute('BEGIN IMMEDIATE')
        # Traverse both directions so superseded history is private as well.
        rows = conn.execute('''WITH RECURSIVE chain(id) AS (
            SELECT event_id FROM financial_events WHERE event_id=?
            UNION SELECT c.child_event_id FROM financial_event_corrections c JOIN chain x ON c.parent_event_id=x.id
            UNION SELECT c.parent_event_id FROM financial_event_corrections c JOIN chain x ON c.child_event_id=x.id
        ) SELECT f.* FROM financial_events f JOIN chain ON f.event_id=chain.id''', (event_id,)).fetchall()
        if not rows or any(r['owner_id'] != actor.user_id or r['space_id'] not in actor.allowed_spaces for r in rows):
            raise PermissionError('Only your own expense can be moved to your private space.')
        active = [r for r in rows if r['status'] in ('ACTIVE', 'PENDING_HUMAN_REVIEW')]
        if len(active) != 1:
            raise ValueError('The quoted expense has no single current record to move.')
        current = active[0]
        ids = [r['event_id'] for r in rows]
        marks = ','.join('?' for _ in ids)
        media = conn.execute(f'''SELECT DISTINCT m.*,i.sender_phone,i.conversation_type
            FROM event_media_links l JOIN media_objects m ON m.media_id=l.media_id
            JOIN inbound_messages i ON i.message_id=m.source_message_id
            WHERE l.event_id IN ({marks})''', ids).fetchall()
        for item in media:
            if item['conversation_type'] != 'DIRECT_DM' or normalize_phone(item['sender_phone']) != actor.phone:
                raise ValueError('The original receipt was shared by another source. Its existing visibility needs review before moving this expense.')
            linked_elsewhere = conn.execute(f'''SELECT 1 FROM event_media_links l
                JOIN financial_events f ON f.event_id=l.event_id WHERE l.media_id=?
                AND f.event_id NOT IN ({marks}) AND (f.space_id<>? OR f.owner_id<>?) LIMIT 1''',
                [item['media_id'], *ids, actor.private_space, actor.user_id]).fetchone()
            saved_elsewhere = conn.execute('''SELECT 1 FROM saved_items WHERE media_id=?
                AND (space_id<>? OR owner_id<>?) LIMIT 1''',
                (item['media_id'], actor.private_space, actor.user_id)).fetchone()
            ledger_elsewhere = conn.execute('''SELECT 1 FROM custom_ledger_entries e
                JOIN custom_ledgers l ON l.ledger_id=e.ledger_id WHERE e.media_id=?
                AND (l.space_id<>? OR l.owner_id<>?) LIMIT 1''',
                (item['media_id'], actor.private_space, actor.user_id)).fetchone()
            if linked_elsewhere or saved_elsewhere or ledger_elsewhere:
                raise ValueError('The receipt is also linked to another shared record. No records were moved; choose how that shared receipt should be handled first.')
        status = 'already_private' if all(r['space_id'] == actor.private_space for r in rows) else 'moved'
        if status == 'moved':
            conn.execute(f'UPDATE financial_events SET space_id=? WHERE event_id IN ({marks})',
                         [actor.private_space, *ids])
            conn.execute('''INSERT INTO financial_visibility_changes
                (source_message_id,event_id,owner_id,private_space,event_ids_json,changed_at_utc)
                VALUES(?,?,?,?,?,?)''', (actor.source_message_id, current['event_id'], actor.user_id,
                    actor.private_space, json.dumps(ids), runtime_clock.utc_iso()))
        conn.commit()
        return {'status': status, 'event_id': current['event_id'], 'amount_minor': current['amount_minor'],
                'currency': current['currency'], 'description': current['description'], 'space_id': actor.private_space}


def validate_outbound(conn, row):
    """Recheck moved originals and financial snapshots at the delivery boundary.

    Previously delivered messages cannot be retracted here. Unsigned old report
    snapshots are conservatively blocked for other recipients after a move.
    The user can request a fresh report from current scoped storage.
    """
    if (row['context_kind'] == 'PRIVACY_DELIVERY_NOTICE' and row['kind'] == 'TEXT'
            and row['text_body'] == EXPIRED_NOTICE and not row['local_path']):
        return
    changes = conn.execute('SELECT * FROM financial_visibility_changes').fetchall()
    for change in changes:
        if _own_destination(conn, change['owner_id'], row['conversation_id']):
            continue
        ids = json.loads(change['event_ids_json'])
        marks = ','.join('?' for _ in ids)
        path = row['local_path']
        if path and conn.execute(f'''SELECT 1 FROM event_media_links l JOIN media_objects m ON m.media_id=l.media_id
            WHERE l.event_id IN ({marks}) AND m.local_path=? LIMIT 1''', [*ids, path]).fetchone():
            raise PrivacyDeliveryError('This attachment is now private; delivery stopped.')
        if row['context_kind'] == 'FINANCIAL_EVENT' and row['context_id'] in ids:
            raise PrivacyDeliveryError('This expense is now private; delivery stopped.')
        # Covers generated reports/aggregates whose files contain an old view.
        if row['source_message_id'] and conn.execute('''SELECT 1 FROM tool_audit
            WHERE source_message_id=? AND julianday(created_at_utc)<=julianday(?)
            AND (tool_name IN ('query_finances','get_transaction_receipt','get_receipt','find_receipts')
                 OR tool_name LIKE '%finance%report%' OR tool_name LIKE '%financial%report%') LIMIT 1''',
                (row['source_message_id'], change['changed_at_utc'])).fetchone():
            raise PrivacyDeliveryError('Financial visibility changed after this result was prepared. Request a fresh report.')

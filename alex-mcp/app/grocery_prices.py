"""Durable, silent grocery price enrichment. No network in list mutations.

Catalogue quotes are estimates, never expense records. Every saved quote is
bound to the exact item revision so a slow lookup cannot price a renamed item.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import db
import runtime_clock
import scope_policy
import mydin_prices
import manjaku_prices
from config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS grocery_price_jobs (
 item_id TEXT PRIMARY KEY REFERENCES shopping_items(item_id) ON DELETE CASCADE,
 revision TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'QUEUED',
 attempts INTEGER NOT NULL DEFAULT 0, next_try TEXT NOT NULL, lease_until TEXT,
 error_code TEXT
);
CREATE TABLE IF NOT EXISTS grocery_product_choices (
 item_id TEXT PRIMARY KEY REFERENCES shopping_items(item_id) ON DELETE CASCADE,
 item_name TEXT NOT NULL, retailer TEXT NOT NULL, product_id TEXT NOT NULL,
 variant_id TEXT
);
CREATE TABLE IF NOT EXISTS grocery_price_quotes (
 quote_id TEXT PRIMARY KEY,
 item_id TEXT NOT NULL REFERENCES shopping_items(item_id) ON DELETE CASCADE,
 revision TEXT NOT NULL, checked_at TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_grocery_quotes ON grocery_price_quotes(item_id,revision,checked_at);
CREATE TABLE IF NOT EXISTS grocery_item_history (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT,
 item_id TEXT NOT NULL REFERENCES shopping_items(item_id) ON DELETE CASCADE,
 occurred_at TEXT NOT NULL, revision TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_grocery_history ON grocery_item_history(item_id,occurred_at);
"""


def revision(row):
    fields = {k: row[k] for k in ('item_name', 'quantity', 'notes')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def changed(conn, item_id, *, remember=True):
    """Called inside the shopping transaction; cheap local writes only."""
    row = conn.execute('SELECT * FROM shopping_items WHERE item_id=?', (item_id,)).fetchone()
    if not row:
        return
    now, rev = db.utc_now(), revision(row)
    if remember:
        payload = {k: row[k] for k in ('item_name', 'quantity', 'notes', 'status', 'space_id')}
        previous = conn.execute(
            'SELECT payload_json FROM grocery_item_history WHERE item_id=? ORDER BY event_id DESC LIMIT 1',
            (item_id,)).fetchone()
        encoded = json.dumps(payload, sort_keys=True)
        if not previous or previous[0] != encoded:
            conn.execute('INSERT INTO grocery_item_history(item_id,occurred_at,revision,payload_json) VALUES(?,?,?,?)',
                         (item_id, now, rev, encoded))
    conn.execute('DELETE FROM grocery_product_choices WHERE item_id=? AND item_name!=?',
                 (item_id, row['item_name']))
    if row['status'] != 'OPEN':
        conn.execute('DELETE FROM grocery_price_jobs WHERE item_id=?', (item_id,))
        return
    conn.execute("""INSERT INTO grocery_price_jobs(item_id,revision,next_try) VALUES(?,?,?)
        ON CONFLICT(item_id) DO UPDATE SET revision=excluded.revision,state='QUEUED',
        attempts=0,next_try=excluded.next_try,lease_until=NULL,error_code=NULL
        WHERE grocery_price_jobs.revision!=excluded.revision""", (item_id, rev, now))


def pack_count(value):
    """Free text quantities are not silently interpreted as package counts."""
    if value is None or not str(value).strip():
        return 1
    m = re.fullmatch(r'\s*(\d+)\s*(?:x|packs?|packets?|bottles?|cartons?|units?|pieces?|pcs|bags?|tins?|boxes?)?\s*',
                     str(value), re.I)
    return int(m[1]) if m and 1 <= int(m[1]) <= 10000 else None


def _query(row):
    return re.sub(r'\b(?:mydin|manjaku|at|from)\b', '', row['item_name'], flags=re.I).strip()[:120]


def _retailers(row):
    text = (row['item_name'] + ' ' + (row['notes'] or '')).lower()
    if 'manjaku' in text:
        return ['manjaku']
    if 'mydin' in text:
        return ['mydin']
    # Retain both retailers as alternatives, never sum both as purchases.
    return ['mydin', 'manjaku']


def _quote(retailer, product_id, variant, quantity, tz):
    result = (mydin_prices.product(product_id, quantity, tz) if retailer == 'mydin'
              else manjaku_prices.product(product_id, variant, quantity, tz))
    rows = result.get('products') or []
    if len(rows) != 1:
        raise ValueError('product_unavailable')
    p = rows[0]
    if p.get('requires_variant_selection'):
        return {'status': 'needs_choice', 'candidates': rows, 'retailer': retailer}
    if retailer == 'mydin':
        price, total = p.get('unit_price_for_quantity'), p.get('total_for_quantity')
    else:
        price, total = (p.get('selected_variant') or {}).get('selling_price'), p.get('listed_items_subtotal')
    if not price or not total:
        return {'status': 'unavailable', 'retailer': retailer, 'candidates': rows}
    return {'status': 'priced', 'retailer': retailer, 'product_id': product_id,
            'variant_id': variant, 'title': p['title'], 'url': p['url'],
            'unit_price': price, 'line_total': total, 'packs': quantity,
            'sufficient_stock': p.get('sufficient_stock'), 'currency': 'MYR',
            'checked_at': result['checked_at'], 'estimate': True,
            'note': result.get('price_scope_note') or p.get('total_note'),
            'product': p}


def lookup(row, choice=None):
    settings, quantity = get_settings(), pack_count(row['quantity'])
    if quantity is None:
        return {'status': 'needs_quantity', 'note': 'Confirm the number of packs; weight/volume is not a pack count.'}
    if choice:
        retailer = choice['retailer']
        if getattr(settings, retailer + '_prices_enabled') is not True:
            return {'status': 'disabled', 'retailer': retailer}
        return _quote(retailer, choice['product_id'], choice['variant_id'], quantity, settings.timezone)
    query, options = _query(row), []
    if len(query) < 2:
        return {'status': 'needs_choice', 'note': 'Provide a product name, brand and pack size.'}
    for retailer in _retailers(row):
        if getattr(settings, retailer + '_prices_enabled') is not True:
            continue
        try:
            result = (mydin_prices.search(query, 5, 1, settings.timezone) if retailer == 'mydin'
                      else manjaku_prices.search(query, 3, settings.timezone))
            candidates = result.get('products') or []
            more = result.get('more_results') or result.get('may_have_more_results')
            # A generic name such as 'rice' cannot identify a brand/pack.
            specific = bool(re.search(r'\d', query)) and len(mydin_prices._terms(query)) >= 2
            if specific and len(candidates) == 1 and not more and not candidates[0].get('requires_variant_selection'):
                product = candidates[0]
                pid = product.get('sku') if retailer == 'mydin' else product['handle']
                options.append(_quote(retailer, pid, None, quantity, settings.timezone))
            else:
                options.append({'retailer': retailer, 'status': 'needs_choice' if candidates else 'no_match',
                                'candidates': candidates, 'checked_at': result['checked_at']})
        except (ValueError, OSError, TimeoutError):
            options.append({'retailer': retailer, 'status': 'unavailable'})
    priced = [x for x in options if x['status'] == 'priced']
    # Default reference is MYDIN when the same specified product is available.
    selected = next((x for x in priced if x['retailer'] == 'mydin'), priced[0] if priced else None)
    if selected:
        return {**selected, 'alternatives': options, 'selection': 'exact_product_reference'}
    return {'status': 'needs_choice' if any(x['status'] == 'needs_choice' for x in options) else 'unavailable',
            'alternatives': options}


def process_one():
    """One leased job per isolated worker tick. Stale completions are discarded."""
    if get_settings().grocery_background_prices is not True:
        return False
    now = runtime_clock.now_utc()
    conn = db.connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        job = conn.execute("""SELECT j.* FROM grocery_price_jobs j JOIN shopping_items s USING(item_id)
            WHERE s.status='OPEN' AND j.next_try<=? AND
            (j.state='QUEUED' OR (j.state='RUNNING' AND j.lease_until<?))
            ORDER BY j.next_try LIMIT 1""", (now.isoformat(), now.isoformat())).fetchone()
        if not job:
            conn.commit()
            return False
        row = dict(conn.execute('SELECT * FROM shopping_items WHERE item_id=?', (job['item_id'],)).fetchone())
        choice = conn.execute('SELECT * FROM grocery_product_choices WHERE item_id=?', (job['item_id'],)).fetchone()
        choice = dict(choice) if choice else None
        lease = (now + timedelta(minutes=5)).isoformat()
        conn.execute("UPDATE grocery_price_jobs SET state='RUNNING',lease_until=?,attempts=attempts+1 WHERE item_id=?",
                     (lease, job['item_id']))
        conn.commit()
    finally:
        conn.close()
    try:
        result = lookup(row, choice)
    except Exception:
        # Do not persist network response bodies, credentials or arbitrary exceptions.
        result = {'status': 'unavailable', 'error_code': 'source_unavailable'}
    conn = db.connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        active = conn.execute('SELECT * FROM grocery_price_jobs WHERE item_id=?', (row['item_id'],)).fetchone()
        current = conn.execute('SELECT * FROM shopping_items WHERE item_id=?', (row['item_id'],)).fetchone()
        if not active or not current or active['lease_until'] != lease or revision(current) != job['revision'] or current['status'] != 'OPEN':
            conn.commit()
            return True
        stamp = db.utc_now()
        conn.execute('INSERT INTO grocery_price_quotes VALUES(?,?,?,?,?)',
                     (str(uuid.uuid4()), row['item_id'], job['revision'], stamp, json.dumps(result)))
        failed = result['status'] == 'unavailable'
        retry = failed and active['attempts'] < 3
        next_try = (runtime_clock.now_utc() + timedelta(minutes=15 * active['attempts'])).isoformat()
        conn.execute("UPDATE grocery_price_jobs SET state=?,next_try=?,lease_until=NULL,error_code=? WHERE item_id=?",
                     ('QUEUED' if retry else 'DONE', next_try, 'source_unavailable' if failed else None, row['item_id']))
        conn.commit()
    finally:
        conn.close()
    return True


def maintenance():
    """Backfill existing OPEN items and refresh daily; never reprice past purchases."""
    conn = db.connect()
    try:
        for row in conn.execute("SELECT * FROM shopping_items WHERE status='OPEN'").fetchall():
            changed(conn, row['item_id'])
        cutoff = (runtime_clock.now_utc() - timedelta(hours=24)).isoformat()
        conn.execute("""UPDATE grocery_price_jobs SET state='QUEUED',attempts=0,next_try=?
            WHERE state='DONE' AND item_id IN (SELECT item_id FROM shopping_items WHERE status='OPEN')
            AND NOT EXISTS (SELECT 1 FROM grocery_price_quotes q WHERE q.item_id=grocery_price_jobs.item_id AND q.checked_at>?)""",
                     (db.utc_now(), cutoff))
        conn.commit()
    finally:
        conn.close()


def _row_for_actor(conn, actor, item_id):
    spaces = scope_policy.read_spaces(actor)
    row = conn.execute('SELECT * FROM shopping_items WHERE item_id=?', (item_id,)).fetchone()
    if not row or row['space_id'] not in spaces:
        raise PermissionError('Grocery item not found in your current scope.')
    return row


def select_product(actor, item_id, retailer, product_id, variant_id=None):
    if retailer not in {'mydin', 'manjaku'}:
        raise ValueError('Choose mydin or manjaku.')
    conn = db.connect()
    try:
        row = dict(_row_for_actor(conn, actor, item_id))
    finally:
        conn.close()
    if row['status'] != 'OPEN':
        raise ValueError('Choose a product for an open grocery item; past purchase references are frozen.')
    quantity = pack_count(row['quantity'])
    if quantity is None:
        raise ValueError('Confirm the number of packs first.')
    if getattr(get_settings(), retailer + '_prices_enabled') is not True:
        raise ValueError('That price source is disabled.')
    result = _quote(retailer, product_id, variant_id, quantity, actor.timezone)
    if result['status'] != 'priced':
        return result
    conn = db.connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        current = _row_for_actor(conn, actor, item_id)
        if revision(current) != revision(row):
            raise ValueError('The grocery item changed; read the list and choose again.')
        conn.execute('INSERT OR REPLACE INTO grocery_product_choices VALUES(?,?,?,?,?)',
                     (item_id, row['item_name'], retailer, product_id, variant_id))
        changed(conn, item_id)
        conn.execute('INSERT INTO grocery_price_quotes VALUES(?,?,?,?,?)',
                     (str(uuid.uuid4()), item_id, revision(row), db.utc_now(), json.dumps(result)))
        conn.execute("UPDATE grocery_price_jobs SET state='DONE',lease_until=NULL WHERE item_id=?", (item_id,))
        conn.commit()
        return {'status': 'selected', 'price': result, 'item_id': item_id}
    finally:
        conn.close()


def attach_prices(actor, items):
    conn = db.connect()
    total, missing = Decimal(0), 0
    try:
        for item in items:
            row = _row_for_actor(conn, actor, item['item_id'])
            q = conn.execute('SELECT * FROM grocery_price_quotes WHERE item_id=? AND revision=? ORDER BY checked_at DESC,rowid DESC LIMIT 1',
                             (row['item_id'], revision(row))).fetchone()
            price = json.loads(q['payload_json']) if q else {'status': 'pending'}
            if price.get('status') == 'unavailable':
                for older in conn.execute('SELECT * FROM grocery_price_quotes WHERE item_id=? AND revision=? ORDER BY checked_at DESC,rowid DESC', (row['item_id'], revision(row))):
                    previous = json.loads(older['payload_json'])
                    if previous.get('status') == 'priced':
                        q, price = older, {**previous, 'refresh_error': 'source_unavailable'}
                        break
            if q:
                price['saved_at'] = q['checked_at']
                price['stale'] = runtime_clock.now_utc() - datetime.fromisoformat(q['checked_at']) > timedelta(hours=24)
            if row['status'] == 'OPEN' and (not q or price.get('stale')):
                changed(conn, row['item_id'], remember=False)
                conn.execute("UPDATE grocery_price_jobs SET state='QUEUED',attempts=0,next_try=? WHERE item_id=? AND state='DONE'", (db.utc_now(), row['item_id']))
            item['price'] = price
            if price.get('status') == 'priced':
                total += Decimal(price['line_total'])
            else:
                missing += 1
        conn.commit()
        return {'items': items, 'estimated_total': f'{total:.2f}', 'currency': 'MYR',
                'unpriced_items': missing, 'complete': missing == 0,
                'note': 'Reference-price estimate for listed quantities, not actual spending. Stale quotes are labelled; delivery excluded.'}
    finally:
        conn.close()


def monthly_summary(actor, period=None):
    period = period or runtime_clock.today(actor.timezone).strftime('%Y-%m')
    if not re.fullmatch(r'\d{4}-\d{2}', period):
        raise ValueError('Use YYYY-MM.')
    start = datetime.strptime(period, '%Y-%m').replace(tzinfo=ZoneInfo(actor.timezone))
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    start_utc, end_utc = start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()
    spaces = scope_policy.read_spaces(actor)
    marks = ','.join('?' for _ in spaces)
    conn = db.connect()
    rows, pending, bought, missing = [], Decimal(0), Decimal(0), 0
    try:
        history = conn.execute(f"""SELECT h.* FROM grocery_item_history h JOIN shopping_items s USING(item_id)
            WHERE s.space_id IN ({marks}) AND h.occurred_at<? ORDER BY h.occurred_at,h.event_id""",
                               [*spaces, end_utc]).fetchall()
        latest, purchase_at, purchase_events, states = {}, {}, {}, {}
        for h in history:
            state = json.loads(h['payload_json'])['status']
            if state == 'PURCHASED' and states.get(h['item_id']) != 'PURCHASED':
                purchase_at[h['item_id']] = h['occurred_at']
                purchase_events[h['item_id']] = h
            states[h['item_id']], latest[h['item_id']] = state, h
        for item_id, h in latest.items():
            item = json.loads(h['payload_json'])
            if item['status'] == 'REMOVED' or (item['status'] == 'PURCHASED' and purchase_at[item_id] < start_utc):
                continue
            if item['status'] == 'PURCHASED':
                h = purchase_events[item_id]
                item = json.loads(h['payload_json'])
            cutoff = min(purchase_at[item_id], end_utc) if item['status'] == 'PURCHASED' else end_utc
            q = conn.execute('SELECT * FROM grocery_price_quotes WHERE item_id=? AND revision=? AND checked_at<=? ORDER BY checked_at DESC,rowid DESC LIMIT 1',
                             (item_id, h['revision'], cutoff)).fetchone()
            price = json.loads(q['payload_json']) if q else {'status': 'unknown'}
            amount = Decimal(price['line_total']) if price.get('status') == 'priced' else None
            if amount is None:
                missing += 1
            elif item['status'] == 'OPEN':
                pending += amount
            else:
                bought += amount
            rows.append({'item_id': item_id, **item, 'reference_price': price,
                         'price_checked_at': q['checked_at'] if q else None})
        actual = conn.execute(f"""SELECT currency,SUM(amount_minor) AS amount,COUNT(*) AS records
            FROM financial_events WHERE space_id IN ({marks}) AND status='ACTIVE' AND event_type='Expense'
            AND category IN ('groceries','kids_supplies') AND event_date_utc>=? AND event_date_utc<? GROUP BY currency""",
                              [*spaces, start_utc, end_utc]).fetchall()
        return {'period': period, 'currency': 'MYR', 'pending_list_estimate': f'{pending:.2f}',
                'purchased_list_reference_estimate': f'{bought:.2f}', 'unpriced_items': missing,
                'recorded_actual_spending': [{'currency': r['currency'], 'amount': f"{Decimal(r['amount'] or 0)/100:.2f}", 'records': r['records']} for r in actual],
                'items': rows, 'note': 'Estimates and recorded expenses are separate views; never add them together. Actual spending covers recorded groceries and kids supplies only; absence of records is not proof of no spending.'}
    finally:
        conn.close()


def main():
    import time
    tick = 0
    while True:
        try:
            if get_settings().grocery_background_prices is True:
                if tick % 60 == 0:
                    maintenance()
                process_one()
        except Exception:
            print('[Alex grocery prices] Background lookup paused; retrying safely.', flush=True)
        tick += 1
        time.sleep(5)


if __name__ == '__main__':
    main()

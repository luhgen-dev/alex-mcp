"""Frozen, owner/chat-bound goal references; display order is never reconstructed."""
from datetime import timedelta
import json
import uuid

import db
import phase2_finance
import runtime_clock
from context import use_actor


def list_for_actor(actor):
    with use_actor(actor):
        rows = phase2_finance.list_goals(actor.phone, actor.conversation_type, 'all')
    ident = str(uuid.uuid4())
    conn = db.connect()
    try:
        conn.execute('''INSERT INTO goal_selection_sets
            (selection_id,user_id,conversation_id,items_json,read_scope,created_at_utc,expires_at_utc)
            VALUES(?,?,?,?,?,?,?)''', (ident, actor.user_id, actor.conversation_id,
            json.dumps([r['goal_id'] for r in rows]), actor.read_scope or 'family',
            db.utc_now(), (runtime_clock.now_utc()+timedelta(hours=48)).isoformat()))
        conn.commit()
    finally:
        conn.close()
    return {'goals': [dict(row, number=i) for i,row in enumerate(rows,1)],
            'selection_id': ident, 'selection_kind': 'GOAL',
            'display_rule': 'Present this exact numbered order with name, target and status. Never show internal IDs.'}


def detail(actor, goal_id):
    with use_actor(actor):
        rows = phase2_finance.list_goals(actor.phone, actor.conversation_type, 'all')
    row = next((r for r in rows if r['goal_id']==goal_id), None)
    if not row:
        raise PermissionError('Goal is unavailable in the current authorized scope.')
    return row


def resolve(actor, goal_id=None, goal_name=None, choice=None, selection_id=None):
    if choice is not None:
        if isinstance(choice, bool) or int(choice) < 1:
            raise ValueError('Choose a displayed goal number.')
        conn = db.connect()
        try:
            sql = 'SELECT * FROM goal_selection_sets WHERE user_id=? AND conversation_id=?'
            args = [actor.user_id, actor.conversation_id]
            if selection_id:
                sql += ' AND selection_id=?'; args.append(selection_id)
            sql += ' ORDER BY created_at_utc DESC,rowid DESC LIMIT 1'
            row = conn.execute(sql,args).fetchone()
        finally:
            conn.close()
        from datetime import datetime
        if not row or datetime.fromisoformat(row['expires_at_utc']) <= runtime_clock.now_utc():
            raise ValueError('That goal list has expired. Please show the goals again.')
        ids = json.loads(row['items_json'])
        if int(choice)>len(ids):
            raise ValueError('Choose a number from the displayed goal list.')
        goal_id = ids[int(choice)-1]
        detail(actor, goal_id)  # revalidate current scope, never inherit old private scope
    with use_actor(actor):
        resolved = phase2_finance.resolve_goal_reference(goal_id,goal_name,actor.phone,actor.conversation_type)
    detail(actor, resolved)
    return resolved

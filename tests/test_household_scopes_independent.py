"""Exercise durable domain state and read boundaries without an LLM or sender."""
import json
import unittest

import independent_fixture
import db
import phase2
import services
import custom_ledgers


class HouseholdScopeTests(independent_fixture.HouseholdFixture):
    def plans(self):
        shared = phase2.create_plan(self.actor('shared', 'Create a family plan'), 'Shared plan')
        private = phase2.create_plan(self.actor('private', 'Create a private plan'), 'Private plan')
        return shared['plan_id'], private['plan_id']

    def test_plan_reads_follow_current_scope(self):
        shared, private = self.plans()
        for key, text, expected in [
            ('family-read', 'Show plans', {shared}),
            ('private-read', 'Show private plans', {private}),
            ('all-read', 'Show shared and private plans', {shared, private}),
        ]:
            with self.subTest(scope=key):
                rows = phase2.list_plans(self.actor(key, text))['plans']
                self.assertEqual({x['plan_id'] for x in rows}, expected)

    def test_plan_reads_cannot_expose_owner_private_to_spouse_or_group(self):
        shared, private = self.plans()
        for key, wife, group in [('spouse', True, False), ('group', False, True)]:
            rows = phase2.list_plans(self.actor(key, 'Show plans', wife=wife, group=group))['plans']
            self.assertEqual({x['plan_id'] for x in rows}, {shared})
        with self.assertRaises(PermissionError):
            phase2.list_plans(self.actor('group-private', 'Show private plans', group=True))

    def test_task_lifecycle_is_idempotent_and_preserves_audit_history(self):
        actor = self.actor('create-task', 'Create a task to buy paint')
        result = phase2.create_task(actor, 'Buy paint', due_local='2026-11-04')
        task = result['task_id']
        self.assertEqual(phase2.create_task(actor, 'Buy paint')['task_id'], task)
        done = self.actor('complete-task', 'Complete the task')
        phase2.complete_task(done, task)
        phase2.complete_task(done, task)
        phase2.reopen_task(self.actor('reopen-task', 'Reopen the task'), task)
        phase2.cancel_task(self.actor('cancel-task', 'Cancel the task'), task)
        with self.assertRaises(ValueError):
            phase2.reopen_task(self.actor('reopen-cancelled', 'Reopen task'), task)
        with db.connect() as conn:
            row = conn.execute('SELECT * FROM tasks WHERE task_id=?', (task,)).fetchone()
            self.assertEqual(row['status'], 'CANCELLED')
            self.assertEqual(row['due_date_local'], '2026-11-04')
            self.assertIsNone(row['due_at_utc'])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
            self.assertEqual([x[0] for x in conn.execute(
                'SELECT event_type FROM task_events ORDER BY rowid')],
                ['CREATED', 'COMPLETED', 'REOPENED', 'CANCELLED'])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM plans').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM reminders').fetchone()[0], 0)

    def test_task_scope_and_spouse_edit_boundary(self):
        shared = phase2.create_task(self.actor('shared'), 'Shared task')['task_id']
        private = phase2.create_task(self.actor('private', 'Create a private task'), 'Private task')['task_id']
        self.assertEqual([x['task_id'] for x in phase2.list_tasks(self.actor('read'))['tasks']], [shared])
        self.assertEqual([x['task_id'] for x in phase2.list_tasks(
            self.actor('private-read', 'Show private tasks'))['tasks']], [private])
        with self.assertRaises(PermissionError):
            phase2.complete_task(self.actor('spouse', wife=True), private)
        with self.assertRaises(PermissionError):
            phase2.update_task(self.actor('group', group=True), private, title='Exposed')
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT title FROM tasks WHERE task_id=?', (private,)).fetchone()[0], 'Private task')

    def test_custom_ledger_scopes_and_idempotent_money_rows(self):
        shared = custom_ledgers.create_ledger(self.actor('shared'), 'Synthetic trip')['ledger_id']
        private = custom_ledgers.create_ledger(self.actor('private', 'Create private ledger'), 'Synthetic private')['ledger_id']
        self.assertEqual([x['ledger_id'] for x in custom_ledgers.list_ledgers(self.actor('read'))['ledgers']], [shared])
        with self.assertRaises(PermissionError):
            custom_ledgers.add_entry(self.actor('spouse', wife=True), private, 'Unauthorised')
        writer = self.actor('entry', 'Record trip spending RM12.35')
        entry = custom_ledgers.add_entry(writer, shared, 'Synthetic purchase', amount=12.35, entry_kind='SPENDING')
        self.assertEqual(custom_ledgers.add_entry(writer, shared, 'Synthetic purchase', amount=12.35, entry_kind='SPENDING')['entry_id'], entry['entry_id'])
        with db.connect() as conn:
            rows = conn.execute('SELECT amount_minor,currency FROM custom_ledger_entries').fetchall()
            self.assertEqual([tuple(x) for x in rows], [(1235, 'MYR')])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM financial_events').fetchone()[0], 0)

    def test_custom_ledger_money_needs_classification_without_writing(self):
        ledger = custom_ledgers.create_ledger(self.actor('ledger'), 'Synthetic')['ledger_id']
        result = custom_ledgers.add_entry(self.actor('money'), ledger, 'Unclassified money', amount=20)
        self.assertEqual(result['status'], 'needs_clarification')
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM custom_ledger_entries').fetchone()[0], 0)

    def test_custom_ledger_rejects_nonfinite_amount_with_actionable_error(self):
        ledger = custom_ledgers.create_ledger(self.actor('ledger'), 'Synthetic')['ledger_id']
        for i, amount in enumerate([float('nan'), float('inf'), float('-inf'), 1e30]):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                custom_ledgers.add_entry(self.actor(f'bad-{i}'), ledger, 'Invalid money',
                                         amount=amount, entry_kind='SPENDING')
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM custom_ledger_entries').fetchone()[0], 0)

    def test_custom_ledger_large_data_is_valid_json_or_rejected_before_write(self):
        ledger = custom_ledgers.create_ledger(self.actor('ledger'), 'Synthetic')['ledger_id']
        try:
            custom_ledgers.add_entry(self.actor('long-data'), ledger, 'Long note', data={'note': 'x' * 21000})
        except ValueError:
            pass
        with db.connect() as conn:
            for row in conn.execute('SELECT data_json FROM custom_ledger_entries'):
                self.assertEqual(json.loads(row[0])['note'], 'x' * 21000)

    def test_saved_notes_scope_and_emoji_not_stored_as_content(self):
        shared = services.save_item(self.actor('shared', 'Save a note'), 'Shared note', 'Shared content')['item_id']
        private = services.save_item(self.actor('private', 'Save this 🔒'), 'Secret 🔒', 'Private content 🔒')['item_id']
        for key, text, expected in [('family-read', 'Show saved notes', shared),
                                    ('private-read', 'Show private notes', private)]:
            found = services.search_saved_items(self.actor(key, text))
            self.assertEqual([x['item_id'] for x in found['matches']], [expected])
        with db.connect() as conn:
            row = conn.execute('SELECT title,content,space_id FROM saved_items WHERE item_id=?', (private,)).fetchone()
            self.assertEqual(tuple(row), ('Secret', 'Private content', 'HUSBAND_PVT'))


if __name__ == '__main__':
    unittest.main()

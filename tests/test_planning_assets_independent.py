"""Money planning and asset boundary tests over isolated durable storage."""
import unittest

import independent_fixture
import db
import phase2_finance as finance
import phase2_library as library
from context import use_actor


class PlanningAssetTests(independent_fixture.HouseholdFixture):
    def test_goal_reads_cannot_be_widened_by_tool_scope_argument(self):
        owner = self.actor('goals', 'Create goals')
        shared = finance.create_goal('Shared target', 100, 10, owner.phone, visibility='family')['goal_id']
        private = finance.create_goal('Private target', 200, 20, owner.phone, visibility='private')['goal_id']
        for key, text, expected in [('family', 'Show goals', {shared}),
                                    ('private', 'Show private goals', {private})]:
            reader = self.actor(key, text)
            with use_actor(reader):
                rows = finance.list_goals(reader.phone, requested_scope='all')
            self.assertEqual({x['goal_id'] for x in rows}, expected)

    def test_private_goal_is_denied_to_spouse_and_group_by_id(self):
        owner = self.actor('goal')
        goal = finance.create_goal('Private target', 100, 10, owner.phone)['goal_id']
        for key, wife, group in [('spouse', True, False), ('group', False, True)]:
            reader = self.actor(key, wife=wife, group=group)
            with self.assertRaises(PermissionError):
                finance.goal_progress(goal, reader.phone, reader.conversation_type)

    def test_pool_spend_retry_and_overdraft_do_not_change_other_finances(self):
        actor = self.actor('pool')
        pool = finance.create_cash_pool('Synthetic stash', actor.phone, opening_balance=100,
                                        event_date='2026-11-02', source_message_id='open')['pool_id']
        result = finance.record_cash_pool_spend(pool, 12.35, '2026-11-02', actor.phone, source_message_id='spend')
        self.assertEqual(result['balance'], 87.65)
        result = finance.record_cash_pool_spend(pool, 12.35, '2026-11-02', actor.phone, source_message_id='spend')
        self.assertEqual(result['status'], 'already_applied')
        with self.assertRaises(ValueError):
            finance.record_cash_pool_spend(pool, 90, '2026-11-02', actor.phone, source_message_id='overdraw')
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT SUM(amount_minor) FROM alex_phase2_cash_pool_adjustments').fetchone()[0], 8765)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM alex_phase2_cash_pool_adjustments').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM financial_events').fetchone()[0], 0)

    def test_private_asset_reads_and_edits_are_isolated(self):
        owner = self.actor('asset')
        shared = library.create_asset('Synthetic shared washer', owner.phone)['asset_id']
        private = library.create_asset('Synthetic private laptop', owner.phone, visibility='private')['asset_id']
        for key, text, expected in [('family', 'Show assets', {shared}),
                                    ('private', 'Show private assets', {private})]:
            reader = self.actor(key, text)
            with use_actor(reader):
                rows = library.list_assets(reader.phone, requested_scope='all')
            self.assertEqual({x['asset_id'] for x in rows}, expected)
        for key, wife, group in [('spouse', True, False), ('group', False, True)]:
            reader = self.actor(key, wife=wife, group=group)
            with self.assertRaises(PermissionError):
                library.update_asset(private, reader.phone, reader.conversation_type, note='Exposed')
        with db.connect() as conn:
            self.assertIsNone(conn.execute('SELECT note FROM alex_phase2_assets WHERE asset_id=?', (private,)).fetchone()[0])

    def test_unknown_sender_cannot_resolve_actor(self):
        with self.assertRaises(PermissionError):
            db.resolve_actor('+60333333333', 'synthetic@s.whatsapp.net', 'DIRECT_DM', 'unknown')

    def test_group_actor_never_contains_private_memberships(self):
        actor = self.actor('group', 'Show shared and private records', group=True)
        self.assertEqual(actor.allowed_spaces, ('FAMILY_SHARED',))


if __name__ == '__main__':
    unittest.main()

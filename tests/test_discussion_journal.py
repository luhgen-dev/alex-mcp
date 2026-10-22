"""EAR-01 / journal regressions on disposable state, not WhatsApp certification."""
import importlib
from dataclasses import replace

from test_conversation_reliability import ConversationFixture
import test_core as core
import discussion_journal as journal
import listening


class DiscussionJournalTests(ConversationFixture):
    def setUp(self):
        super().setUp()
        journal.ensure_schema()
        conn = core.db.connect()
        conn.execute('DELETE FROM discussion_selection_sets')
        conn.commit()
        conn.close()

    def group(self, mid='ear-start', text='@Alex', spouse=False):
        return self.actor(mid, text, spouse=spouse, conversation='120363000000@g.us')

    def captured(self):
        actor = self.group()
        session = listening.start(actor)['session_id']
        listening.capture(actor, 'ear-first', 'Maybe tidy the workshop this weekend.')
        listening.capture(actor, 'ear-second', 'Agreed Saturday. I will sort the shelves. No task or reminder.')
        listening.stop(actor)
        return actor, session

    def test_stop_commits_fallback_and_recall_after_restart(self):
        actor, sid = self.captured()
        importlib.reload(listening)
        result = listening.recall(self.group('ear-recall', 'Most recent Ear session'), 'most recent Ear session')
        self.assertEqual(result['session_id'], sid)
        self.assertIn('Agreed Saturday', result['summary'])
        self.assertEqual(len(result['messages']), 2)
        conn = core.db.connect()
        for table in ('tasks', 'reminders', 'plans', 'diary_events'):
            self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)
        conn.close()

    def test_natural_topic_and_plural_list_are_grounded(self):
        actor, sid = self.captured()
        result = listening.recall(actor, 'What did we agree about the workshop?')
        self.assertEqual(result['session_id'], sid)
        listed = listening.recall(actor, 'show us our discussions')
        self.assertEqual(listed['discussions'][0]['session_id'], sid)
        self.assertEqual(journal.resolve_choice(actor, 1, listed['selection_id'])['session_id'], sid)
        self.assertEqual(listening.recall(actor, 'discussion about unrelated dolphins')['status'], 'not_found')

    def test_journal_scope_group_and_owner_private_are_enforced(self):
        actor, sid = self.captured()
        other_group = replace(actor, conversation_id='120363999999@g.us')
        with self.assertRaises(PermissionError):
            journal.detail(other_group, sid)
        with self.assertRaises(PermissionError):
            journal.detail(replace(actor, allowed_spaces=()), sid)
        # Private fixture represents an owner-authorized private discussion.
        conn = core.db.connect()
        conn.execute('UPDATE discussion_journal SET space_id=? WHERE session_id=?', (actor.private_space, sid))
        conn.commit()
        conn.close()
        with self.assertRaises(PermissionError):
            journal.detail(actor, sid)
        owner_dm = replace(self.actor('owner-private', 'my private discussion'), read_scope='private')
        self.assertEqual(journal.detail(owner_dm, sid)['session_id'], sid)
        spouse_dm = replace(self.actor('spouse-private', 'my private discussion', spouse=True), read_scope='private')
        with self.assertRaises(PermissionError):
            journal.detail(spouse_dm, sid)

    def test_status_action_history_and_retry_are_durable(self):
        actor, sid = self.captured()
        actor = self.group('ear-action', 'Keep sorting shelves as a discussion action for me')
        added = journal.update_action(actor, sid, title='Sort shelves', owner_id=actor.user_id)
        action_id = added['updated_action_id']
        journal.update_action(actor, sid, title='Sort shelves', owner_id=actor.user_id)
        self.assertEqual(len(journal.detail(actor, sid)['actions']), 1)
        self.assertEqual(len(journal.detail(actor, sid)['history']), 1)
        done_actor = self.group('ear-complete', 'I finished sorting the shelves')
        done = journal.update_action(done_actor, sid, action_id=action_id, status='DONE')
        self.assertEqual(done['actions'][0]['status'], 'DONE')
        self.assertEqual(len(done['history']), 2)
        with self.assertRaises(ValueError):
            journal.update_action(done_actor, sid, action_id=action_id, status='ACKNOWLEDGED')
        importlib.reload(journal)
        self.assertEqual(journal.detail(done_actor, sid)['actions'][0]['status'], 'DONE')

    def test_decisions_and_unresolved_revision_are_explicit(self):
        actor, sid = self.captured()
        changed = journal.update_discussion(self.group('ear-decision', 'Not decided yet'), sid,
            subject='Workshop', status='DECISION_PENDING', unresolved=['Choose a date'], decisions=[])
        self.assertEqual(changed['progress_status'], 'DECISION_PENDING')
        self.assertEqual(changed['unresolved'], ['Choose a date'])
        with self.assertRaises(ValueError):
            journal.update_discussion(self.group('ear-stale', 'Agreed'), sid, status='DONE', expected_revision=1)
        self.assertEqual(journal.detail(actor, sid)['progress_status'], 'DECISION_PENDING')

    def test_numbered_binding_cannot_be_reused_by_other_sender_or_group(self):
        actor, sid = self.captured()
        listed = journal.list_discussions(actor)
        spouse = self.group('ear-spouse', 'show 1', spouse=True)
        with self.assertRaises(ValueError):
            journal.resolve_choice(spouse, 1, listed['selection_id'])
        with self.assertRaises(ValueError):
            journal.resolve_choice(replace(actor, conversation_id='other@g.us'), 1, listed['selection_id'])
        with self.assertRaises(ValueError):
            journal.resolve_choice(actor, 2, listed['selection_id'])

    def test_capture_retry_does_not_duplicate_original_evidence(self):
        actor = self.group()
        sid = listening.start(actor)['session_id']
        listening.capture(actor, 'same-message', 'Original evidence')
        listening.capture(actor, 'same-message', 'Changed retry must not rewrite original')
        listening.stop(actor)
        result = journal.detail(actor, sid)
        self.assertEqual([m['text'] for m in result['messages']], ['Original evidence'])

    def test_no_action_owner_invention_or_cross_discussion_update(self):
        actor, sid = self.captured()
        with self.assertRaises(ValueError):
            journal.update_action(actor, sid, title='Sort shelves', owner_id='unknown-user')
        with self.assertRaises(ValueError):
            journal.update_action(actor, sid, action_id='not-an-action', status='DONE')
        self.assertEqual(journal.detail(actor, sid)['actions'], [])

    def test_generic_numbered_resolution_uses_journal_not_stale_saved_list(self):
        actor, sid = self.captured()
        core.services.save_item(replace(actor, action_key='ear-note'), 'Other note', 'unrelated', shared=True)
        core.services.search_saved_items(actor, '')
        listed = journal.list_discussions(actor)
        latest = core.services.latest_selection_set_context(actor)
        self.assertEqual(latest['kind'], 'DISCUSSION')
        self.assertEqual(latest['id'], listed['selection_id'])
        self.assertEqual(core.services.resolve_numbered_choice(actor, 1)['session_id'], sid)
        self.assertEqual(core.services.resolve_numbered_choice(actor, 1, 'DISCUSSION', listed['selection_id'])['session_id'], sid)
        self.assertEqual(core.services.latest_single_selection_context(actor)['id'], sid)
        import conversation_state
        state = conversation_state.build(actor, 'Show 1')
        self.assertEqual(state['latest_list']['kind'], 'DISCUSSION')

    def test_real_new_process_reads_persisted_capture_actions_and_history(self):
        import subprocess
        import sys
        import os
        import json
        from dataclasses import asdict
        actor, sid = self.captured()
        journal.update_action(self.group('restart-action', 'Sort shelves'), sid, title='Sort shelves', owner_id=actor.user_id)
        env = dict(os.environ)
        env['ALEX_DATA_DIR'] = os.path.dirname(core.db.DB_PATH)
        env['ALEX_OPTIONS_PATH'] = core.OPTIONS
        env['PYTHONPATH'] = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'alex-mcp', 'app'))
        script = (
            'import json,sys; from context import ActorContext; import discussion_journal; '
            'a=ActorContext(**json.loads(sys.argv[1])); '
            'print(json.dumps(discussion_journal.detail(a,sys.argv[2])))'
        )
        result = subprocess.run([sys.executable, '-c', script, json.dumps(asdict(actor)), sid],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        restored = json.loads(result.stdout)
        self.assertEqual(restored['session_id'], sid)
        self.assertEqual(len(restored['messages']), 2)
        self.assertEqual(restored['actions'][0]['title'], 'Sort shelves')
        self.assertEqual(len(restored['history']), 1)

    def test_replay_old_action_update_cannot_undo_completion(self):
        actor, sid = self.captured()
        creator = self.group('action-create', 'Sort shelves')
        created = journal.update_action(creator, sid, title='Sort shelves', owner_id=actor.user_id)
        action_id = created['updated_action_id']
        working = self.group('action-working', 'I am sorting shelves')
        journal.update_action(working, sid, action_id=action_id, status='IN_PROGRESS')
        journal.update_action(self.group('action-done', 'Finished'), sid, action_id=action_id, status='DONE')
        journal.update_action(working, sid, action_id=action_id, status='IN_PROGRESS')
        journal.update_action(creator, sid, title='Sort shelves', owner_id=actor.user_id)
        self.assertEqual(journal.detail(actor, sid)['actions'][0]['status'], 'DONE')
        self.assertEqual(len(journal.detail(actor, sid)['history']), 3)

    def test_structured_stop_summary_has_evidence_actions_and_no_promotion(self):
        actor, sid = self.captured()
        payload = {'summary': 'Agreed to sort shelves; no task or reminder.', 'subject': 'Workshop',
                   'decisions': [{'text': 'Sort shelves', 'evidence_message_ids': ['ear-second']}],
                   'unresolved': [], 'actions': [
                       {'title': 'Sort shelves', 'status': 'ACTION_PENDING', 'owner_id': actor.user_id,
                        'due_local': None, 'evidence_message_ids': ['ear-second']}]}
        result = journal.accept_structured_summary(actor, sid, payload)
        self.assertEqual(result['review_state'], 'STRUCTURED')
        self.assertEqual(result['decisions'], ['Sort shelves'])
        self.assertEqual(result['actions'][0]['owner_id'], actor.user_id)
        action_id = result['actions'][0]['action_id']
        journal.update_action(self.group('later-done', 'Done'), sid, action_id=action_id, status='DONE')
        journal.accept_structured_summary(actor, sid, payload)
        self.assertEqual(journal.detail(actor, sid)['actions'][0]['status'], 'DONE')
        conn = core.db.connect()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)
        conn.close()

    def test_structured_summary_rejects_invented_evidence_owner_or_deadline_atomically(self):
        actor, sid = self.captured()
        base = {'summary': 'Interpretation', 'actions': [
            {'title': 'Sort shelves', 'status': 'ACTION_PENDING', 'owner_id': actor.user_id,
             'due_local': None, 'evidence_message_ids': ['ear-second']}]}
        import copy
        for field, value in [('evidence_message_ids', ['other-session-message']), ('owner_id', 'not-a-participant'),
                             ('due_local', '2099-12-31')]:
            payload = copy.deepcopy(base)
            payload['actions'][0][field] = value
            with self.assertRaises(ValueError):
                journal.accept_structured_summary(actor, sid, payload)
            self.assertEqual(journal.detail(actor, sid)['review_state'], 'UNSTRUCTURED')
            self.assertEqual(journal.detail(actor, sid)['actions'], [])

    def test_promotion_link_needs_consent_and_same_scope(self):
        actor, sid = self.captured()
        action = journal.update_action(actor, sid, title='Sort shelves')['updated_action_id']
        creator = replace(self.group('task-link', 'Create task'), action_key='task-link')
        task = core.phase2.create_task(creator, 'Sort shelves', shared=True)
        self.assertTrue(journal.link_action(actor, sid, action, 'TASK', task['task_id'])['nothing_written'])
        linked = journal.link_action(actor, sid, action, 'TASK', task['task_id'], confirmed=True)
        self.assertEqual(linked['actions'][0]['linked_id'], task['task_id'])
        with self.assertRaises(PermissionError):
            journal.link_action(actor, sid, action, 'TASK', 'nonexistent', confirmed=True)

    def test_generic_goal_selection_uses_frozen_goal_order(self):
        import goal_references
        from context import use_actor
        actor = replace(self.actor('goal-integration', 'Create a family goal'), action_key='goal-integration')
        with use_actor(actor):
            created = core.phase2_finance.create_goal('Synthetic telescope', 800, 40, actor.phone,
                conversation_type=actor.conversation_type, visibility='family', status='DRAFT')
            listed = goal_references.list_for_actor(actor)
            self.assertEqual(core.services.latest_selection_set_context(actor)['kind'], 'GOAL')
            self.assertEqual(core.services.resolve_numbered_choice(actor, 1)['goal_id'], created['goal_id'])
            self.assertEqual(core.services.get_selection_target(actor, 'GOAL', created['goal_id'])['goal_id'], created['goal_id'])
            import conversation_state
            self.assertEqual(conversation_state.build(actor, 'show 1')['latest_list']['kind'], 'GOAL')

    def test_structured_weekday_due_uses_captured_timestamp(self):
        actor = self.group()
        sid = listening.start(actor)['session_id']
        listening.capture(actor, 'dated-decision', 'Agreed Saturday, I will sort the shelves.', '2026-09-03T09:00:00+00:00')
        listening.stop(actor)
        payload = {'summary': 'Saturday shelves', 'actions': [
            {'title': 'Sort shelves', 'status': 'ACTION_PENDING', 'owner_id': actor.user_id,
             'due_local': '2026-09-05', 'evidence_message_ids': ['dated-decision']}]}
        self.assertEqual(journal.accept_structured_summary(actor, sid, payload)['actions'][0]['due_local'], '2026-09-05')

    def test_summary_provider_tool_request_cannot_execute_household_actions(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import patch, Mock
        actor, sid = self.captured()
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='', tool_calls=[
                SimpleNamespace(function=SimpleNamespace(name='create_task', arguments='{}'))]))], usage=None)
        with patch.object(core.brain, '_provider_routes', return_value=[{'provider': 'grok', 'model': 'test'}]), \
             patch.object(core.brain, '_client_for', return_value=client), \
             patch.object(core.brain, '_completion_kwargs', return_value={}) as kwargs, \
             patch.object(core.brain, '_record_usage_buckets'):
            with self.assertRaises(ValueError):
                asyncio.run(core.brain.summarize_discussion(actor, 'Return summary JSON', {}))
            self.assertEqual(kwargs.call_args.args[2], [])
        conn = core.db.connect()
        for table in ('tasks', 'diary_events', 'reminders', 'plans'):
            self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)
        conn.close()

    def test_summary_projection_exposes_grounded_message_ids_and_dates(self):
        context = {'listening_context': {'messages': [
            {'message_id': 'captured-id', 'user_id': 'known-speaker', 'text': 'Tentative only',
             'created_at_utc': '2026-09-03T09:00:00+00:00'}]}}
        prompt = core.brain._quoted_context_message(context)
        self.assertIn('captured-id', prompt)
        self.assertIn('2026-09-03T09:00:00+00:00', prompt)
        self.assertIn('known-speaker', prompt)

    def test_action_metadata_edit_preserves_done_and_due_requires_source_grounding(self):
        actor, sid = self.captured()
        aid = journal.update_action(actor, sid, title='Sort shelves')['updated_action_id']
        journal.update_action(self.group('done-meta', 'Finished'), sid, action_id=aid, status='DONE')
        result = journal.update_action(self.group('rename-meta', 'Rename it Shelf sorting'), sid,
                                       action_id=aid, title='Shelf sorting')
        self.assertEqual(result['actions'][0]['status'], 'DONE')
        with self.assertRaises(ValueError):
            journal.update_action(self.group('invent-date', 'Rename it'), sid, action_id=aid, due_local='2026-12-20')
        result = journal.update_action(self.group('dated-meta', 'Set the date to 20 December 2026'), sid,
                                       action_id=aid, due_local='2026-12-20')
        self.assertEqual(result['actions'][0]['due_local'], '2026-12-20')
        self.assertEqual(result['actions'][0]['status'], 'DONE')

    def test_empty_journal_result_does_not_resurrect_old_saved_selection(self):
        actor, sid = self.captured()
        core.services.save_item(replace(actor, action_key='old-note'), 'Other note', 'unrelated', shared=True)
        core.services.search_saved_items(actor, '')
        listed = journal.list_discussions(actor, 'dolphins')
        self.assertEqual(listed['status'], 'not_found')
        import conversation_state
        state = conversation_state.build(actor, 'Show 1')
        self.assertEqual(state['latest_list']['kind'], 'DISCUSSION')
        self.assertEqual(state['latest_list']['items'], [])
        with self.assertRaises(ValueError):
            core.services.resolve_numbered_choice(actor, 1)

    def test_pending_list_excludes_completed_and_other_owner_only_actions(self):
        actor, sid = self.captured()
        added = journal.update_action(actor, sid, title='Sort shelves', owner_id=actor.user_id)
        journal.update_action(self.group('pending-done', 'Done'), sid,
            action_id=added['updated_action_id'], status='DONE')
        self.assertEqual(journal.list_discussions(actor, pending_only=True)['discussions'], [])
        spouse = self.group('pending-spouse', 'I will paint', spouse=True)
        journal.update_action(spouse, sid, title='Paint', owner_id=spouse.user_id)
        self.assertEqual(journal.list_discussions(actor, pending_only=True)['discussions'], [])
        self.assertEqual(len(journal.list_discussions(spouse, pending_only=True)['discussions']), 1)
        journal.update_discussion(self.group('unresolved-pending', 'Date undecided'), sid, unresolved=['Choose date'])
        self.assertEqual(len(journal.list_discussions(actor, pending_only=True)['discussions']), 1)

    def test_uncertain_or_negated_weekday_is_not_a_deadline(self):
        actor = self.group()
        for text in ["Maybe Saturday", "Next Saturday", "Not on Saturday",
                     "We haven't decided a date yet, possibly Saturday"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                journal._grounded_due('2026-09-05', [{'text': text,
                    'created_at_utc': '2026-09-03T09:00:00+00:00'}], actor)

    def test_explicit_rejected_dates_are_not_grounded_in_summary_or_action_update(self):
        rejected = [
            'Do not schedule it for 5 September 2026.',
            'The deadline is not 2026-09-05.',
            'We cancelled the date of 5 September 2026.',
            '5 September 2026 is no longer the deadline.',
            'We are undecided about 2026-09-05.',
            "Don't set the date to 5 September 2026.",
            'The deadline moved from 5 September 2026 to 7 September 2026.',
        ]
        for index, text in enumerate(rejected):
            with self.subTest(text=text):
                actor = self.group('rejected-start-' + str(index))
                sid = listening.start(actor)['session_id']
                message_id = 'rejected-date-' + str(index)
                listening.capture(actor, message_id, text)
                listening.stop(actor)
                payload = {'summary': text, 'actions': [
                    {'title': 'Sort shelves', 'status': 'ACTION_PENDING',
                     'owner_id': actor.user_id, 'due_local': '2026-09-05',
                     'evidence_message_ids': [message_id]}]}
                with self.assertRaises(ValueError):
                    journal.accept_structured_summary(actor, sid, payload)
                saved = journal.detail(actor, sid)
                self.assertEqual(saved['review_state'], 'UNSTRUCTURED')
                self.assertEqual(saved['actions'], [])
                self.assertIn(text, saved['summary'])
                action = journal.update_action(self.group('date-action-' + str(index), 'Sort shelves'),
                                               sid, title='Sort shelves')['updated_action_id']
                before = journal.detail(actor, sid)
                with self.assertRaises(ValueError):
                    journal.update_action(self.group('date-update-' + str(index), text), sid,
                                          action_id=action, due_local='2026-09-05')
                after = journal.detail(actor, sid)
                self.assertIsNone(after['actions'][0]['due_local'])
                self.assertEqual(after['history'], before['history'])

    def test_later_date_retraction_cannot_be_bypassed_by_earlier_affirmation(self):
        actor = self.group()
        evidence = [
            {'text': 'Agreed 5 September 2026.', 'created_at_utc': '2026-09-01T12:00:00+00:00'},
            {'text': 'The deadline is not 2026-09-05.', 'created_at_utc': '2026-09-01T12:01:00+00:00'},
        ]
        for rows in (evidence, list(reversed(evidence))):
            with self.assertRaises(ValueError):
                journal._grounded_due('2026-09-05', rows, actor)

    def test_affirmed_date_survives_unrelated_no_promotion_or_object_negation(self):
        actor = self.group()
        for text in ['Agreed 5 September 2026; do not add a task or diary.',
                     'Sort shelves Saturday, not the boxes.',
                     'Saturday 5 September 2026; no reminder or task.']:
            with self.subTest(text=text):
                self.assertEqual(journal._grounded_due('2026-09-05', [{
                    'text': text, 'created_at_utc': '2026-09-03T09:00:00+00:00'}], actor), '2026-09-05')

    def test_final_affirmed_date_supersedes_prior_tentative_evidence(self):
        actor = self.group()
        rows = [
            {'text': 'Maybe 5 September 2026.', 'created_at_utc': '2026-09-01T12:00:00+00:00'},
            {'text': 'Agreed 5 September 2026; do not create a task.',
             'created_at_utc': '2026-09-01T12:01:00+00:00'},
        ]
        self.assertEqual(journal._grounded_due('2026-09-05', rows, actor), '2026-09-05')

    def test_same_timestamp_conflicting_dates_fail_closed_in_either_reference_order(self):
        actor = self.group()
        rows = [
            {'text': 'The deadline is not 2026-09-05.', 'created_at_utc': '2026-09-01T12:00:00+00:00'},
            {'text': 'Agreed 5 September 2026.', 'created_at_utc': '2026-09-01T12:00:00+00:00'},
        ]
        for evidence in (rows, list(reversed(rows))):
            with self.assertRaises(ValueError):
                journal._grounded_due('2026-09-05', evidence, actor)
        # Different textual offsets denoting the same instant are still tied.
        equivalent = [dict(rows[0], created_at_utc='2026-09-01T20:00:00+08:00'), rows[1]]
        for evidence in (equivalent, list(reversed(equivalent))):
            with self.assertRaises(ValueError):
                journal._grounded_due('2026-09-05', evidence, actor)
        # A genuinely later explicit affirmation resolves earlier uncertainty.
        later = [rows[0], dict(rows[1], created_at_utc='2026-09-01T12:00:01+00:00')]
        for evidence in (later, list(reversed(later))):
            self.assertEqual(journal._grounded_due('2026-09-05', evidence, actor), '2026-09-05')

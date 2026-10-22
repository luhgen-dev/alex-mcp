"""Issue 61 synthetic runtime regressions; no provider or WhatsApp delivery."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from test_conversation_reliability import ConversationFixture, core, db, services


class Issue61TaskReminderTests(ConversationFixture):
    def setUp(self):
        super().setUp()
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        clock = patch.object(core.runtime_clock, "now_utc", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def actor_now(self, mid, text, **kwargs):
        return replace(self.actor(mid, text, **kwargs), received_at_utc=self.now.isoformat(), action_key=mid + ':action')

    def test_heads_up_retains_task_time_and_identity_from_durable_state(self):
        creator = self.actor_now('i61-task', 'Add a task: polish the bike tomorrow')
        task = core.phase2.create_task(creator, 'polish the bike', due_local=(self.now + timedelta(days=1)).date().isoformat())
        asker = self.actor_now('i61-ask', 'For polish the bike task remind me in 30 minutes')
        due = (self.now + timedelta(minutes=30)).isoformat()
        response = core.mcp_server.create_task_reminder(asker, due, task_id=task['task_id'])
        self.assertEqual(response['status'], 'needs_clarification')
        pending = db.single_pending_item(asker, 'REMINDER_DRAFT', max_age_seconds=1800)
        self.assertEqual(services.task_reminder_draft_payload(asker, pending)['task_id'], task['task_id'])
        # New actor + fresh DB connections simulate absence of process memory.
        answer = self.actor_now('i61-answer', 'Just a heads-up')
        result = core.ingress._resume_task_reminder_choice(answer, answer.trusted_text)
        self.assertTrue(result['task_reminder_created'])
        conn = db.connect()
        try:
            row = conn.execute('SELECT * FROM tasks WHERE task_id=?', (task['task_id'],)).fetchone()
            self.assertIsNone(row['due_at_utc'])
            links = conn.execute('SELECT * FROM task_reminder_links WHERE task_id=?', (task['task_id'],)).fetchall()
            self.assertEqual(len(links), 1)
            reminder = conn.execute('SELECT * FROM reminders WHERE reminder_id=?', (links[0]['reminder_id'],)).fetchone()
            self.assertEqual(datetime.fromisoformat(reminder['due_at_utc']), self.now + timedelta(minutes=30))
            self.assertEqual(conn.execute('SELECT status FROM pending_items WHERE item_id=?',(pending['item_id'],)).fetchone()[0], 'RESOLVED')
        finally:
            conn.close()

    def test_quoted_task_choice_cannot_cross_sender(self):
        actor, draft, _ = self.draft()
        other = self.actor_now('other-choice', 'Just heads up', spouse=True)
        self.assertIsNone(services.task_reminder_draft_payload(other, draft))
        self.assertIsNone(core.ingress._resume_task_reminder_choice(other, other.trusted_text, self.quote(draft)))

    def test_draft_never_pins_and_cleans_legacy_pin(self):
        actor, draft, outbound = self.draft()
        conn = db.connect()
        try:
            conn.execute("UPDATE outbound_messages SET delivery_status='SENT',provider_message_id='synthetic-draft' WHERE outbound_id=?",(outbound,))
            conn.commit()
            with patch.object(core.outbox, '_send', return_value=(True, '{}')) as send:
                core.outbox._reconcile_pending_item_markers(conn)
                send.assert_not_called()
            conn.execute("UPDATE outbound_messages SET job_pinned_at_utc=CURRENT_TIMESTAMP,job_pin_target='OUTBOUND' WHERE outbound_id=?",(outbound,))
            conn.commit()
            with patch.object(core.outbox, '_send', return_value=(True, '{}')) as send:
                core.outbox._reconcile_pending_item_markers(conn)
                self.assertEqual(send.call_args[0][0]['kind'], 'unpin')
        finally:
            conn.close()

    def test_claim_before_due_does_not_postpone_delivery(self):
        actor = self.actor_now('claimable-create', 'Remind us in 20 minutes', conversation='synthetic@g.us')
        with patch.object(services, '_family_group_conversation_id', return_value='synthetic@g.us'):
            created = services.create_reminder(actor, 'water plants', (self.now + timedelta(minutes=20)).isoformat(), destination='group', claimable=True, presence_aware=False)
        claimant = self.actor_now('claim-before-due', "I'll handle this one")
        result = services.claim_reminder(claimant, created['reminder_id'])
        self.assertEqual(result['status'], 'claimed')
        conn = db.connect()
        try:
            row = conn.execute('SELECT * FROM reminders WHERE reminder_id=?',(created['reminder_id'],)).fetchone()
            self.assertIsNone(row['next_delivery_at_utc'])
        finally:
            conn.close()
        with patch.object(core.runtime_clock, 'now_utc', return_value=self.now + timedelta(minutes=21)):
            core.scheduler.fire_due()
        conn = db.connect()
        try:
            due = conn.execute("SELECT * FROM outbound_messages WHERE context_kind='REMINDER_INITIAL' AND context_id=?",(created['reminder_id'],)).fetchone()
            self.assertIsNotNone(due)
            self.assertEqual(due['conversation_id'], claimant.conversation_id)
        finally:
            conn.close()

    def test_local_date_and_menu_and_no_hallucinated_draft(self):
        self.assertIn('Asia/Kuala_Lumpur', services._friendly_reminder_time('2026-11-02T11:00:00+00:00','Asia/Kuala_Lumpur'))
        self.assertIn('Mon', services._friendly_reminder_time('2026-11-02T11:00:00+00:00','Asia/Kuala_Lumpur'))
        for query in ('Show me menu', 'Show me your menu', 'Open the menu please'):
            self.assertTrue(core.ingress._menu_command(query))
        actor = self.actor_now('leave-question','What is my leave balance?')
        self.assertIsNone(core.ingress._maybe_create_reminder_draft(actor, 'What time should I remind you?'))

    def test_task_provenance_is_original_not_current_read(self):
        creator = self.actor_now('original-task','Add a task to polish the bike')
        task = core.phase2.create_task(creator, 'polish the bike')
        reader = self.actor_now('task-read','Do I have any tasks?')
        detail = core.phase2.task_detail(reader,task['task_id'])
        self.assertEqual(detail['original_instruction'],creator.trusted_text)
        rendered = core.phase2.render_task_detail(detail)
        self.assertNotIn(reader.trusted_text,rendered)
        self.assertIn('Linked reminder: None.',rendered)

    def test_post_due_release_posts_fresh_claimable_group_card(self):
        actor = self.actor_now('postdue-create', 'Remind us in 10 minutes')
        with patch.object(services, '_family_group_conversation_id', return_value='synthetic@g.us'):
            created = services.create_reminder(actor, 'water plants', (self.now + timedelta(minutes=10)).isoformat(), destination='group', claimable=True, presence_aware=False)
        services.claim_reminder(actor, created['reminder_id'])
        later = self.now + timedelta(minutes=11)
        with patch.object(core.runtime_clock, 'now_utc', return_value=later):
            core.scheduler.fire_due()
            result = services.release_reminder_claim(self.actor_now('postdue-release', 'put it back in the group'), created['reminder_id'])
        self.assertTrue(result['fresh_family_card'])
        conn = db.connect()
        try:
            row = conn.execute('SELECT * FROM reminders WHERE reminder_id=?',(created['reminder_id'],)).fetchone()
            self.assertEqual(row['status'],'OPEN')
            self.assertIsNone(row['claimed_by_user_id'])
            self.assertEqual(datetime.fromisoformat(row['next_delivery_at_utc']), later)
            cards = conn.execute("SELECT * FROM outbound_messages WHERE context_kind='REMINDER_SETUP' AND context_id=? ORDER BY rowid",(created['reminder_id'],)).fetchall()
            self.assertEqual(len(cards),2)
            fresh = cards[-1]
            self.assertEqual(fresh['conversation_id'],'synthetic@g.us')
            conn.execute("UPDATE outbound_messages SET delivery_status='SENT',provider_message_id='postdue-fresh',delivered_at_utc=CURRENT_TIMESTAMP WHERE outbound_id=?",(fresh['outbound_id'],))
            conn.commit()
            with patch.object(core.outbox, '_send', return_value=(True,'{}')):
                core.outbox._reconcile_claim_setup_markers(conn)
            marked = conn.execute('SELECT * FROM outbound_messages WHERE outbound_id=?',(fresh['outbound_id'],)).fetchone()
            self.assertIsNotNone(marked['job_pinned_at_utc'])
            self.assertIsNotNone(marked['job_reacted_at_utc'])
        finally:
            conn.close()

    def test_regenerated_brief_outbounds_share_ack_and_cleanup(self):
        with patch.object(core.brain,'respond', side_effect=AssertionError('brief must not use model')):
            for n in (1,2):
                result = core.ingress.process(self.payload(f'brief-{n}', 'Show my daily brief'))
                self.assertTrue(result['daily_brief'])
        conn=db.connect()
        try:
            rows=conn.execute("SELECT * FROM outbound_messages WHERE context_kind='DAILY_BRIEF' ORDER BY rowid").fetchall()
            self.assertEqual(len(rows),2)
            self.assertEqual(rows[0]['context_id'],rows[1]['context_id'])
            for n,row in enumerate(rows,1):
                conn.execute("UPDATE outbound_messages SET provider_message_id=?,delivery_status='SENT',delivered_at_utc=CURRENT_TIMESTAMP WHERE outbound_id=?",(f'brief-wa-{n}',row['outbound_id']))
            conn.commit()
            with patch.object(core.outbox,'_send',return_value=(True,'{}')):
                core.outbox._reconcile_daily_brief_markers(conn)
            ack=core.ingress.process(self.payload('brief-ack','',reaction_target_message_id='brief-wa-2',reaction_text='✅',event_kind='REACTION'))
            self.assertEqual(ack['reaction']['status'], 'acknowledged')
            with patch.object(core.outbox,'_send',return_value=(True,'{}')):
                core.outbox._reconcile_daily_brief_markers(conn)
            marked=conn.execute("SELECT * FROM outbound_messages WHERE context_kind='DAILY_BRIEF'").fetchall()
            for row in marked:
                if row['job_pinned_at_utc']:
                    self.assertIsNotNone(row['job_unpinned_at_utc'])
        finally:
            conn.close()

    def test_quoted_natural_claim_does_not_ask_for_new_time(self):
        actor=self.actor_now('quote-claim-create','Remind us in 20 minutes')
        with patch.object(services,'_family_group_conversation_id',return_value='synthetic@g.us'):
            created=services.create_reminder(actor,'water plants',(self.now+timedelta(minutes=20)).isoformat(),destination='group',claimable=True,presence_aware=False)
        conn=db.connect()
        try:
            conn.execute("UPDATE outbound_messages SET provider_message_id='claim-card',delivery_status='SENT' WHERE context_kind='REMINDER_SETUP' AND context_id=?",(created['reminder_id'],))
            conn.commit()
        finally: conn.close()
        with patch.object(core.brain,'respond',side_effect=AssertionError('claim must bind exact quoted reminder')):
            result=core.ingress.process(self.payload('quoted-claim',"I'll handle this one",conversation_id='synthetic@g.us',conversation_type='GROUP',is_mentioned=True,quoted_message_id='claim-card'))
        self.assertTrue(result.get('reminder_claimed'),result)
        conn=db.connect()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM reminders').fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pending_items WHERE kind='REMINDER_DRAFT'").fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT claimed_by_user_id FROM reminders').fetchone()[0],actor.user_id)
            dm=conn.execute("SELECT conversation_id FROM outbound_messages WHERE context_kind='REMINDER_CLAIM_CONFIRMED'").fetchone()
            self.assertEqual(dm[0],actor.conversation_id)
        finally: conn.close()

    def test_heads_up_swipe_reply_recovers_without_provider_call(self):
        creator=self.actor_now('swipe-task','Add a task to check tires tomorrow')
        task=core.phase2.create_task(creator,'check tires',due_local=(self.now+timedelta(days=1)).date().isoformat())
        asker=self.actor_now('swipe-task-ask','For check tires task remind me in 30 minutes')
        response=core.mcp_server.create_task_reminder(asker,(self.now+timedelta(minutes=30)).isoformat(),task_id=task['task_id'])
        oid=db.queue_outbound(asker.conversation_id,'TEXT',text=response['clarification_question'],source_message_id=asker.source_message_id,context_kind='PENDING_ITEM',context_id=response['pending_item_id'])
        conn=db.connect()
        try:
            conn.execute("UPDATE outbound_messages SET delivery_status='SENT',provider_message_id='task-choice-card' WHERE outbound_id=?",(oid,));conn.commit()
        finally:conn.close()
        with patch.object(core.brain,'respond',side_effect=AssertionError('grounded closed choice needs no model')):
            result=core.ingress.process(self.payload('swipe-task-answer','Just a heads-up',quoted_message_id='task-choice-card'))
        self.assertTrue(result.get('task_reminder_created'),result)
        conn=db.connect()
        try:
            linked=conn.execute('SELECT reminder_id FROM task_reminder_links WHERE task_id=?',(task['task_id'],)).fetchall()
            self.assertEqual(len(linked),1)
            self.assertEqual(conn.execute("SELECT status FROM pending_items WHERE item_id=?",(response['pending_item_id'],)).fetchone()[0],'RESOLVED')
        finally:conn.close()

    def test_claim_confirmation_is_atomic_and_retry_safe(self):
        actor=self.actor_now('atomic-create','Remind us in 20 minutes',conversation='synthetic@g.us')
        with patch.object(services,'_family_group_conversation_id',return_value='synthetic@g.us'):
            created=services.create_reminder(actor,'water plants',(self.now+timedelta(minutes=20)).isoformat(),destination='group',claimable=True,presence_aware=False)
        claimant=self.actor_now('atomic-claim',"I'll handle this one")
        conn=db.connect()
        try:
            conn.execute("CREATE TRIGGER synthetic_fail_confirmation BEFORE INSERT ON outbound_messages WHEN NEW.context_kind='REMINDER_CLAIM_CONFIRMED' BEGIN SELECT RAISE(ABORT,'synthetic queue failure'); END")
            conn.commit()
        finally:conn.close()
        with self.assertRaises(Exception):
            services.claim_reminder_with_confirmation(claimant,created['reminder_id'])
        conn=db.connect()
        try:
            self.assertIsNone(conn.execute('SELECT claimed_by_user_id FROM reminders WHERE reminder_id=?',(created['reminder_id'],)).fetchone()[0])
            conn.execute('DROP TRIGGER synthetic_fail_confirmation');conn.commit()
        finally:conn.close()
        first=services.claim_reminder_with_confirmation(claimant,created['reminder_id'])
        again=services.claim_reminder_with_confirmation(claimant,created['reminder_id'])
        self.assertEqual(first['confirmation_outbound_id'],again['confirmation_outbound_id'])
        self.assertEqual(first['confirmation_delivery_status'],'PENDING')

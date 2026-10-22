"""Synthetic ingress checks: scripted intent, real authorized tool and durable state."""
from unittest.mock import patch
from test_conversation_reliability import ConversationFixture, core, db
import progressive_teaching


class TeachingIngressTests(ConversationFixture):
    def sent_card(self, actor, provider, **kwargs):
        oid=db.queue_outbound(actor.conversation_id,'TEXT',text='Could you clarify?',source_message_id=actor.source_message_id,**kwargs)
        conn=db.connect()
        try:
            conn.execute("UPDATE outbound_messages SET delivery_status='SENT',provider_message_id=? WHERE outbound_id=?",(provider,oid));conn.commit()
        finally:conn.close()
        return oid

    def test_swipe_correction_verified_recovery_then_explicit_learning(self):
        original=self.actor('teach-original','Put cereal on our list')
        self.sent_card(original,'teach-original-card')
        result, requests = self.scripted([
            {"tool":"add_shopping_item","args":{"item":"cereal"}},
            {"content":"Added cereal."}],
            payload=self.payload('teach-correction','No, I meant add cereal to shopping',quoted_message_id='teach-original-card'))
        self.assertTrue(result['ok'])
        conn=db.connect()
        try:
            draft=conn.execute('SELECT * FROM teaching_corrections WHERE correction_message_id=?',('teach-correction',)).fetchone()
            self.assertEqual(draft['state'],'RECOVERED')
            cards=conn.execute("SELECT * FROM outbound_messages WHERE context_kind='TEACHING_CONFIRMATION' AND context_id=?",(draft['draft_id'],)).fetchall()
            self.assertEqual(len(cards),1)
            conn.execute("UPDATE outbound_messages SET delivery_status='SENT',provider_message_id='teach-confirm-card' WHERE outbound_id=?",(cards[0]['outbound_id'],));conn.commit()
        finally:conn.close()
        current=self.actor('teach-not-yet','Put cereal on our list')
        self.assertIsNone(progressive_teaching.lookup_hint(current,'shopping'))
        with patch.object(core.brain,'respond',side_effect=AssertionError('approval is deterministic')):
            result=core.ingress.process(self.payload('teach-approve','remember this correction',quoted_message_id='teach-confirm-card'))
        self.assertTrue(result['ok'])
        current=self.actor('teach-later','Put cereal on our list')
        self.assertIsNotNone(progressive_teaching.lookup_hint(current,'shopping'))
        other=self.actor('teach-other','Put cereal on our list',spouse=True)
        self.assertIsNone(progressive_teaching.lookup_hint(other,'shopping'))

    def test_no_quote_clarifies_without_model_or_learning(self):
        with patch.object(core.brain,'respond',side_effect=AssertionError('must clarify binding')):
            result=core.ingress.process(self.payload('teach-unbound','No, I meant add cereal'))
        self.assertTrue(result['teaching'])
        conn=db.connect()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM shopping_items').fetchone()[0],0)
        finally:conn.close()

"""Real ingress/brain imports under CI's pinned application dependencies."""
import asyncio
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch
from tests import test_core as core
from context import ActorContext

class DotIngressTests(unittest.TestCase):
    def test_dot_route_precedes_legacy_semantics_and_preserves_authenticated_actor(self):
        actor = ActorContext('USR_HUSBAND','+60111111111',('FAMILY_SHARED','HUSBAND_PVT'),
            'HUSBAND_PVT','dot-test-dm','DIRECT_DM','dot-test-msg',(),'Asia/Kuala_Lumpur')
        settings = replace(core.brain.get_settings(),ai_provider='dot')
        payload={'message_id':actor.source_message_id,'conversation_id':actor.conversation_id,
                 'conversation_type':'DIRECT_DM','sender_phone':actor.phone,'text':'milk pls'}
        with patch.object(core.db,'claim_inbound',return_value='CLAIMED'), \
             patch.object(core.db,'touch_inbound_processing'), \
             patch.object(core.db,'resolve_actor',return_value=actor), \
             patch.object(core.media,'process_payload_media',return_value=([],[],[])), \
             patch.object(core.ingress,'get_settings',return_value=settings), \
             patch.object(core.ingress,'_finish_simple_turn',return_value={'ok':True}) as finish, \
             patch.object(core.ingress,'_recover_reminder_draft_context') as semantics, \
             patch.object(core.brain,'respond',new_callable=AsyncMock) as legacy, \
             patch('dot_runtime.submit',return_value='pending') as submit:
            self.assertEqual(core.ingress.process(payload),{'ok':True})
            self.assertEqual(submit.call_args.args[0].user_id,actor.user_id)
            self.assertEqual(submit.call_args.args[0].phone,actor.phone)
            self.assertEqual(submit.call_args.args[1],'milk pls')
            semantics.assert_not_called()
            legacy.assert_not_called()
            self.assertTrue(finish.call_args.kwargs['dot_experimental'])

    def test_direct_legacy_brain_call_in_dot_mode_fails_before_provider(self):
        settings=replace(core.brain.get_settings(),ai_provider='dot')
        actor=ActorContext('USR_HUSBAND','+60111111111',('FAMILY_SHARED',),'HUSBAND_PVT',
                           'dot-test-dm','dot-test','dot-test-msg',(),'Asia/Kuala_Lumpur')
        with patch.object(core.brain,'get_settings',return_value=settings), \
             patch.object(core.brain,'_history_user_text',side_effect=AssertionError('legacy path reached')):
            reply,attachments=asyncio.run(core.brain.respond(actor,'milk pls'))
        self.assertIn('no action was taken',reply)
        self.assertEqual(attachments,[])

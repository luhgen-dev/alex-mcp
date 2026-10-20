import base64
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'alex-mcp' / 'app'))
from context import ActorContext
from dot_bridge import DotBridge, BridgeError
from dot_events import EventSubscriptions
from dot_http import dispatch

class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'test.db')
        with sqlite3.connect(self.path) as c:
            c.execute('CREATE TABLE outbound_messages(outbound_id TEXT PRIMARY KEY,source_message_id TEXT,conversation_id TEXT,kind TEXT,text_body TEXT)')
        self.now = 1000000.
        self.enabled = True
        self.revoked = False
        self.calls = []
        self.actor = ActorContext('USR_HUSBAND','+601111','FAMILY_SHARED HUSBAND_PVT'.split(),'HUSBAND_PVT','chat1','DIRECT_DM','msg1',(),'Asia/Kuala_Lumpur',source='text',trusted_text='milk pls',read_scope='family')
        self.events = EventSubscriptions(self.path, authorized=lambda p:self.enabled and p=='owner',
            sender=lambda u,b,h:(200, json.dumps({'challenge':json.loads(b).get('challenge')}).encode()),clock=lambda:self.now)
        self.events.subscribe('owner', {'name':'alex.request.created','arguments':{},'delivery':{'mode':'webhook','url':'https://callback.example/event','secret':'whsec_'+base64.b64encode(b'x'*32).decode()}})
        def resolve(*args):
            if self.revoked: raise PermissionError('revoked')
            return replace(self.actor,phone=args[0],conversation_id=args[1],conversation_type=args[2],source_message_id=args[3])
        def execute(actor,tool,args):
            self.calls.append((actor,tool,args))
            return {'status':'added','item':'milk'}
        self.bridge = DotBridge(self.path,principal='owner',enabled=lambda:self.enabled,
                                resolve_actor=resolve,execute=execute,events=self.events,clock=lambda:self.now)

    def submit(self, text='milk pls', actor=None):
        return self.bridge.enqueue(actor or self.actor,text)['request_id']

    def test_roundtrip_one_write_one_reply_and_replay(self):
        rid=self.submit()
        self.assertEqual(self.bridge.read('owner',rid)['text'],'milk pls')
        a={'item':'milk'}
        first=self.bridge.call('owner',rid,'add1','add_shopping_item',a)
        self.assertEqual(first,self.bridge.call('owner',rid,'add1','add_shopping_item',a))
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0][0].allowed_spaces,('FAMILY_SHARED',))
        self.assertTrue(self.calls[0][0].action_key.startswith('dot:'))
        receipt=self.bridge.complete('owner',rid,'Added milk.')
        self.assertEqual(receipt,self.bridge.complete('owner',rid,'Added milk.'))
        with sqlite3.connect(self.path) as c:self.assertEqual(c.execute('SELECT count(*) FROM outbound_messages').fetchone()[0],1)
        self.assertEqual(first,self.bridge.call('owner',rid,'another','add_shopping_item',a))
        self.assertEqual(len(self.calls),1)
        with self.assertRaises(BridgeError):self.bridge.call('owner',rid,'new-item','add_shopping_item',{'item':'bread'})

    def test_actor_destination_and_scope_cannot_be_overridden(self):
        rid=self.submit()
        for extra in [{'actor':'wife'},{'shared':False},{'scope':'private'},{'conversation_id':'other'}]:
            with self.assertRaises(BridgeError):self.bridge.call('owner',rid,'a','add_shopping_item',{'item':'milk',**extra})
        with self.assertRaises(PermissionError):self.bridge.read('intruder',rid)
        self.revoked=True
        with self.assertRaises(PermissionError):self.bridge.read('owner',rid)

    def test_expiration_and_out_of_order(self):
        old=self.submit()
        new=self.submit('bread',replace(self.actor,source_message_id='msg2'))
        self.assertEqual(self.bridge.read('owner',old)['state'],'SUPERSEDED')
        with self.assertRaises(BridgeError):self.bridge.call('owner',old,'a','add_shopping_item',{'item':'milk'})
        self.now+=181
        with self.assertRaises(BridgeError):self.bridge.complete('owner',new,'Added bread')
        self.assertEqual(self.bridge.read('owner',new)['state'],'EXPIRED')

    def test_uncertain_execution_never_retried_or_fallen_back(self):
        rid=self.submit()
        def crash(*args): self.calls.append(args);raise RuntimeError('after effect')
        self.bridge.execute=crash
        for aid in ['a','a','different']:
            with self.assertRaises(BridgeError):self.bridge.call('owner',rid,aid,'add_shopping_item',{'item':'milk'})
        self.assertEqual(len(self.calls),1)
        with self.assertRaises(BridgeError):self.bridge.complete('owner',rid,'Success')
        with self.assertRaises(BridgeError):self.submit('again',replace(self.actor,source_message_id='msg2'))
        self.assertEqual(self.bridge.read('owner',rid)['actions'][0]['state'],'UNCERTAIN')

    def test_duplicate_source_conflict_and_action_conflict(self):
        rid=self.submit()
        self.assertEqual(self.submit(),rid)
        with self.assertRaises(BridgeError):self.submit('different')
        self.bridge.call('owner',rid,'a','add_shopping_item',{'item':'milk'})
        with self.assertRaises(BridgeError):self.bridge.call('owner',rid,'a','add_shopping_item',{'item':'bread'})

    def test_private_group_and_audio_unsupported(self):
        for changes in [{'read_scope':'private'},{'conversation_type':'GROUP'},{'source':'voice'}]:
            with self.assertRaises(BridgeError):self.submit(actor=replace(self.actor,**changes))
        self.enabled=False
        with self.assertRaises(PermissionError):self.submit()

    def test_mutation_replay_with_new_action_id_is_cached(self):
        rid=self.submit()
        args={'item_id':'shared-item','quantity':'2'}
        result=self.bridge.call('owner',rid,'first','update_shopping_item',args)
        self.assertEqual(result,self.bridge.call('owner',rid,'new-id','update_shopping_item',args))
        with self.assertRaises(BridgeError):self.bridge.call('owner',rid,'new-id','update_shopping_item',{**args,'quantity':'3'})
        self.assertEqual(len(self.calls),1)

    def test_no_second_dot_and_no_replay_to_replacement(self):
        from dot_events import EventError
        rid=self.submit()
        params={'name':'alex.request.created','arguments':{},'delivery':{'mode':'webhook','url':'https://other.example/event','secret':'whsec_'+base64.b64encode(b'y'*32).decode()}}
        with self.assertRaises(EventError):self.events.subscribe('owner',params)
        self.events.unsubscribe('owner',{'name':'alex.request.created','arguments':{},'delivery':{'mode':'webhook','url':'https://callback.example/event'}})
        self.events.subscribe('owner',params)
        self.bridge.publish_pending()
        with sqlite3.connect(self.path) as c:self.assertEqual(c.execute('SELECT count(*) FROM dot_event_deliveries').fetchone()[0],1)
        with self.assertRaises(BridgeError):self.bridge.call('owner',rid,'a','add_shopping_item',{'item':'milk'})

    def test_expired_completed_action_releases_conversation(self):
        rid=self.submit()
        self.bridge.call('owner',rid,'list','list_shopping_items',{})
        self.now+=181
        self.bridge.expire_pending()
        self.assertEqual(self.bridge.read('owner',rid)['state'],'EXPIRED')
        self.assertTrue(self.submit('bread',replace(self.actor,source_message_id='next')))
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM outbound_messages').fetchone()[0],1)
        self.bridge.expire_pending()
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM outbound_messages').fetchone()[0],1)

    def test_revoked_expiry_never_sends_results(self):
        rid=self.submit()
        self.bridge.call('owner',rid,'a','add_shopping_item',{'item':'milk'})
        self.revoked=True
        self.now+=181
        self.bridge.expire_pending()
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM outbound_messages').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT state FROM dot_requests').fetchone()[0],'REVOKED')

    def test_publication_crash_recovered_from_durable_request(self):
        from unittest.mock import patch
        with patch.object(self.events,'publish',side_effect=RuntimeError('simulated crash')):
            rid=self.submit()
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM dot_event_deliveries').fetchone()[0],0)
        self.bridge.publish_pending()
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM dot_event_deliveries').fetchone()[0],1)
        self.assertEqual(self.bridge.read('owner',rid)['state'],'PENDING')

    def test_jsonrpc_and_unknown_tools_fail_closed(self):
        request={'jsonrpc':'2.0','id':1,'method':'tools/list'}
        self.assertEqual(len(dispatch(self.bridge,'owner',request)['result']['tools']),3)
        self.assertIn('error',dispatch(self.bridge,'other',request))
        request.update(method='tools/call',params={'name':'delete_everything','arguments':{}})
        self.assertIn('error',dispatch(self.bridge,'owner',request))
        self.assertEqual(self.calls,[])

if __name__=='__main__':unittest.main()

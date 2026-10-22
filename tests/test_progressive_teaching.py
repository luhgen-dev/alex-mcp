"""Post-repair teaching fallback: scoped hints, explicit consent, no replay."""
import json
import sqlite3
from unittest.mock import patch
from dataclasses import replace

import test_core as core
from test_conversation_reliability import ConversationFixture
import progressive_teaching as teaching


class ProgressiveTeachingTests(ConversationFixture):
    def setUp(self):
        super().setUp()
        teaching.ensure_schema()
        with core.db.connect() as conn:
            conn.execute("DELETE FROM teaching_history")
            conn.execute("DELETE FROM teaching_corrections")

    def stage(self, marker="one", original="last detergent", correction="No, I meant add detergent to shopping", context=None):
        source = self.actor(marker + "-source", original)
        context = context or {}
        oid = core.db.queue_outbound(source.conversation_id,"TEXT",text="What do you mean?",
            source_message_id=source.source_message_id, context_kind=context.get("kind"), context_id=context.get("id"))
        actor = self.actor(marker + "-correction", correction)
        row = teaching.prepare_correction(actor, {"outbound_id": oid})
        return actor, row, oid

    def verified(self, actor, draft_id, status="OK", tool="add_shopping_item", result=None):
        with core.db.connect() as conn:
            if core.brain._is_mutating_tool(tool):
                conn.execute("INSERT OR IGNORE INTO tool_execution_claims(action_key,tool_name,state) VALUES(?,?,'COMPLETED')",(actor.source_message_id + "-action",tool))
            conn.execute("""INSERT INTO tool_audit(audit_id,action_key,source_message_id,user_id,
                tool_name,arguments_json,result_json,status) VALUES(?,?,?,?,?,'{}',?,?)""",
                (actor.source_message_id + "-audit", actor.source_message_id + "-action",actor.source_message_id,
                 actor.user_id,tool,json.dumps(result or {"status":"added","item_id":"synthetic-item"}),status))
        return teaching.record_recovery(actor, draft_id)

    def approve(self, actor, staged, marker="approve", text="remember this correction"):
        prompt = core.db.queue_outbound(actor.conversation_id,"TEXT",text="Remember this correction?",
            source_message_id=actor.source_message_id,context_kind="TEACHING_CONFIRMATION",context_id=staged["draft_id"])
        approver = self.actor(marker,text)
        return teaching.approve_correction(approver,{"outbound_id":prompt}), prompt, approver

    def test_only_explicit_quote_correction_stages_no_tool_call_or_auto_learning(self):
        actor = self.actor("ordinary", "add detergent")
        self.assertIsNone(teaching.prepare_correction(actor, None))
        actor = self.actor("unbound", "No, I meant detergent")
        self.assertEqual(teaching.prepare_correction(actor,None)["status"], "clarify")
        actor, staged, _ = self.stage()
        self.assertEqual(staged["status"], "recovery_hint")
        self.assertNotIn("tool", staged["hint"])
        self.assertNotIn("arguments", staged["hint"])
        self.assertEqual(teaching.record_recovery(actor,staged["draft_id"])["status"],"not_learned")
        result,_,_ = self.approve(actor,staged)
        self.assertEqual(result["status"],"not_learned")
        self.assertIsNone(teaching.lookup_hint(self.actor("repeat","last detergent"),"shopping"))
        with core.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM shopping_items").fetchone()[0],0)

    def test_verified_then_explicit_approval_exact_match_restart_and_idempotency(self):
        actor,staged,_ = self.stage()
        self.assertEqual(self.verified(actor,staged["draft_id"])["status"],"awaiting_approval")
        self.assertIsNone(teaching.lookup_hint(self.actor("before","last detergent"),"shopping"))
        result,prompt,approver = self.approve(actor,staged)
        self.assertEqual(result["status"],"approved")
        self.assertEqual(teaching.approve_correction(approver,{"outbound_id":prompt})["version"],1)
        teaching.ensure_schema()  # restart-compatible durable state
        hint=teaching.lookup_hint(self.actor("after","  Last   detergent "),"shopping")
        self.assertEqual(hint["interpretation"],"add detergent to shopping")
        self.assertIn("Hint only",hint["authority"])
        self.assertIsNone(teaching.lookup_hint(self.actor("different","last milk"),"shopping"))
        self.assertIsNone(teaching.lookup_hint(self.actor("other-domain","last detergent"),"reminders"))
        with core.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT confidence FROM teaching_corrections").fetchone()[0],"user_confirmed_tool_verified")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM teaching_history WHERE state='APPROVED'").fetchone()[0],1)

    def test_scope_owner_chat_and_object_binding_cannot_change(self):
        actor,staged,oid=self.stage(context={"kind":"TASK","id":"task-A"})
        other=self.actor("other-author","No, I meant add detergent",spouse=True)
        with self.assertRaises(PermissionError):
            teaching.prepare_correction(other,{"outbound_id":oid})
        self.verified(actor,staged["draft_id"])
        self.approve(actor,staged)
        reader=self.actor("same","last detergent")
        self.assertIsNotNone(teaching.lookup_hint(reader,"shopping",{"kind":"TASK","id":"task-A"}))
        self.assertIsNone(teaching.lookup_hint(reader,"shopping",{"kind":"TASK","id":"task-B"}))
        self.assertIsNone(teaching.lookup_hint(self.actor("spouse-read","last detergent",spouse=True),"shopping",{"kind":"TASK","id":"task-A"}))
        self.assertIsNone(teaching.lookup_hint(replace(reader,conversation_id="another-chat"),"shopping",{"kind":"TASK","id":"task-A"}))
        self.assertIsNone(teaching.lookup_hint(replace(reader,read_scope="private"),"shopping",{"kind":"TASK","id":"task-A"}))

    def test_conflict_requires_separate_explicit_replacement_and_retains_history(self):
        actor,one,_=self.stage("first")
        self.verified(actor,one["draft_id"])
        self.approve(actor,one,"first-approval")
        actor,two,_=self.stage("second",correction="No, I meant mark detergent bought")
        self.verified(actor,two["draft_id"])
        result,prompt,_=self.approve(actor,two,"second-approval")
        self.assertEqual(result["status"],"collision")
        self.assertEqual(teaching.lookup_hint(self.actor("before-replace","last detergent"),"shopping")["interpretation"],"add detergent to shopping")
        consent=self.actor("replace-consent","replace the saved correction")
        approved=teaching.approve_correction(consent,{"outbound_id":prompt})
        self.assertEqual(approved["version"],2)
        self.assertEqual(teaching.lookup_hint(self.actor("after-replace","last detergent"),"shopping")["interpretation"],"mark detergent bought")
        with core.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM teaching_corrections WHERE state='APPROVED'").fetchone()[0],2)

    def test_actual_software_error_is_diagnostic_not_phrase_or_retry(self):
        source=self.actor("error-source","last detergent")
        with core.db.connect() as conn:
            conn.execute("UPDATE inbound_messages SET processing_state='FAILED',last_error='synthetic backend exception' WHERE message_id=?",(source.source_message_id,))
        oid=core.db.queue_outbound(source.conversation_id,"TEXT",text="Processing error",source_message_id=source.source_message_id)
        actor=self.actor("error-correction","No, I meant add detergent to shopping")
        result=teaching.prepare_correction(actor,{"outbound_id":oid})
        self.assertEqual(result["status"],"software_error")
        self.assertTrue(result["error_id"].startswith("ALEX-"))
        retry=teaching.prepare_correction(actor,{"outbound_id":oid})
        self.assertEqual(result["error_id"],retry["error_id"])
        self.assertIsNone(teaching.lookup_hint(self.actor("no-error-rule","last detergent"),"shopping"))
        with core.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_reported_errors").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM shopping_items").fetchone()[0],0)

    def test_wrong_or_uncertain_tool_result_cannot_be_learned(self):
        actor,staged,_=self.stage()
        result=self.verified(actor,staged["draft_id"],result={"status":"needs_confirmation","nothing_written":True})
        self.assertEqual(result["status"],"not_learned")
        result,_,_=self.approve(actor,staged)
        self.assertEqual(result["status"],"not_learned")

    def test_private_original_requires_current_explicit_private_authorization(self):
        actor,staged,_=self.stage(original="private spare detergent")
        self.assertEqual(staged["status"],"clarify")
        with core.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM teaching_corrections").fetchone()[0],0)

    def test_confirmation_is_atomic_retry_safe_and_marked_queued_not_delivered(self):
        actor,staged,_=self.stage()
        with core.db.connect() as conn:
            conn.execute("""CREATE TRIGGER reject_teaching_prompt BEFORE INSERT ON outbound_messages
              WHEN NEW.context_kind='TEACHING_CONFIRMATION' BEGIN SELECT RAISE(ABORT,'synthetic queue failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.verified(actor,staged["draft_id"])
        with core.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT state FROM teaching_corrections").fetchone()[0],"STAGED")
            conn.execute("DROP TRIGGER reject_teaching_prompt")
        first=teaching.record_recovery(actor,staged["draft_id"])
        again=teaching.record_recovery(actor,staged["draft_id"])
        self.assertEqual(first["confirmation_outbound_id"],again["confirmation_outbound_id"])
        self.assertEqual(first["reply_delivery"],"queued")
        with core.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbound_messages WHERE context_kind='TEACHING_CONFIRMATION'").fetchone()[0],1)

    def test_unique_domain_lookup_is_one_read_query_and_conflicts_fail_closed(self):
        actor,staged,_=self.stage("one-domain")
        self.verified(actor,staged["draft_id"])
        self.approve(actor,staged)
        reader=self.actor("read-domain","last detergent")
        conn=core.db.connect()
        statements=[]
        conn.set_trace_callback(statements.append)
        with patch.object(teaching.db,"connect",return_value=conn):
            self.assertEqual(teaching.lookup_for_turn(reader)["domain"],"shopping")
        self.assertEqual(len(statements),1)
        self.assertTrue(statements[0].lstrip().startswith("SELECT"))
        conn.close()
        actor,other,_=self.stage("two-domains",correction="No, I meant show my reminders")
        self.verified(actor,other["draft_id"],tool="list_reminders",result={"reminders":[]})
        self.approve(actor,other,"approve-second-domain")
        self.assertIsNone(teaching.lookup_for_turn(reader))
        self.assertIsNone(teaching.lookup_for_turn(replace(reader,read_scope="all")))

    def test_error_report_marker_crash_recovers_without_duplicate_report(self):
        source=self.actor("crash-source","last detergent")
        with core.db.connect() as conn:
            conn.execute("UPDATE inbound_messages SET processing_state='FAILED',last_error='synthetic' WHERE message_id=?",(source.source_message_id,))
        oid=core.db.queue_outbound(source.conversation_id,"TEXT",text="Error",source_message_id=source.source_message_id)
        actor=self.actor("crash-correction","No, I meant add detergent")
        original=teaching.prepare_correction(actor,{"outbound_id":oid})
        with core.db.connect() as conn:
            conn.execute("UPDATE teaching_corrections SET error_id=NULL")
        retried=teaching.prepare_correction(actor,{"outbound_id":oid})
        self.assertEqual(original["error_id"],retried["error_id"])
        with core.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_reported_errors").fetchone()[0],1)

    def test_uncertain_write_claim_cannot_prove_recovery(self):
        actor,staged,_=self.stage()
        with core.db.connect() as conn:
            conn.execute("INSERT INTO tool_execution_claims(action_key,tool_name,state) VALUES(?,'add_shopping_item','UNCERTAIN')",(actor.source_message_id+'-action',))
        self.assertEqual(self.verified(actor,staged["draft_id"])["status"],"not_learned")

    def test_mutation_correction_does_not_learn_from_read_preflight(self):
        actor,staged,_=self.stage("goal-update",original="change my goal",correction="No, I meant update the target on my holiday goal")
        result=self.verified(actor,staged["draft_id"],tool="planning_list_goals",result={"goals":[{"name":"holiday","target":100}]})
        self.assertEqual(result["status"],"not_learned")
        with core.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT state FROM teaching_corrections").fetchone()[0],"STAGED")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbound_messages WHERE context_kind='TEACHING_CONFIRMATION'").fetchone()[0],0)

    def test_clarification_contracts_never_prove_recovery(self):
        for number,result in enumerate((
            {"status":"needs_clarification"}, {"status":"requires_choice"},
            {"requires_choice":True}, {"needs_input":True}, {"ok":False},
            {"result":{"status":"needs_confirmation"}}, {"status":"awaiting_selection"},
        )):
            actor,staged,_=self.stage("clarify-"+str(number),correction="No, I meant update my holiday goal")
            self.assertEqual(self.verified(actor,staged["draft_id"],tool="planning_update_goal",result=result)["status"],"not_learned")

    def test_read_only_correction_can_recover_from_read_without_write(self):
        actor,staged,_=self.stage("reading",correction="No, I meant show my goals")
        self.assertEqual(self.verified(actor,staged["draft_id"],tool="planning_list_goals",result={"goals":[]})["status"],"awaiting_approval")

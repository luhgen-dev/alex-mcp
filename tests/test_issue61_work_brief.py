"""Synthetic WORK/BRIEF acceptance; no live household profile or transport."""
import json
import os
from dataclasses import replace
from unittest.mock import patch

import test_core as core
from test_conversation_reliability import ConversationFixture
import daily_brief


def profile():
    return {
        "roster": {"id": "synthetic", "name": "Synthetic rotation", "cycle_start": "2025-02-03",
                   "pattern": "morning,evening", "day_start": "08:30", "day_end": "17:30",
                   "evening_start": "17:30", "evening_end": "02:00", "effective_from": "2025-01-01"},
        "overtime": {"id": "synthetic", "name": "Synthetic OT", "effective_from": "2025-01-01",
                     "morning_pre_start": "06:30", "morning_pre_end": "08:30", "morning_pre_hours": 2,
                     "morning_weekend_standard_start": "06:00", "morning_weekend_standard_end": "18:00",
                     "evening_post_start": "02:00", "evening_post_end": "05:00", "evening_post_hours": 3,
                     "evening_weekend_days": "saturday", "evening_weekend_standard_start": "17:00",
                     "evening_weekend_standard_end": "05:00"},
        "leave": [{"id": name, "name": name.replace('_', ' ').title(), "entitlement_days": 20 + i,
                   "used_days": i + 2, "as_of_date": "2025-01-01"}
                  for i, name in enumerate(("annual_leave", "childcare_leave", "medical_leave",
                                           "wellness_leave", "hospitalisation_leave", "doil_leave"))],
    }


class Issue61WorkBriefTests(ConversationFixture):
    def owner(self, mid="owner"):
        return replace(self.actor(mid, "my private work profile"), read_scope="private")

    def load(self, actor=None, data=None):
        actor = actor or self.owner()
        return core.profile_config.import_user_work_profile(actor, json.dumps(data or profile()))

    def test_private_import_all_categories_and_scope(self):
        owner = self.owner()
        self.load(owner)
        with core.use_actor(owner):
            rows = core.phase2_work.leave_balances(owner.phone, "DIRECT_DM", "2025-03-01")
        self.assertEqual(len(rows["balances"]), 6)
        self.assertTrue(all(r["remaining_days"] == 18 for r in rows["balances"]))
        spouse = replace(self.actor("spouse", "my private leave", spouse=True), read_scope="private")
        with core.use_actor(spouse):
            self.assertEqual(core.phase2_work.leave_balances(spouse.phone)["status"], "not_configured")
        group = replace(owner, conversation_type="GROUP", conversation_id="synthetic@g.us")
        with self.assertRaises(PermissionError):
            self.load(group)
        with core.use_actor(group), self.assertRaises(PermissionError):
            core.phase2_work.leave_balances(group.phone, "GROUP")

    def test_mcp_own_work_question_uses_private_profile_not_family_scope(self):
        owner = self.owner()
        self.load(owner)
        ordinary = replace(owner, read_scope="family", trusted_text="what is my leave entitlement?")
        with core.use_actor(ordinary):
            balances = core.mcp_server.work_leave_balance(actor=ordinary, as_of_date="2025-03-01")
        self.assertEqual(len(balances["balances"]), 6)
        roster = replace(ordinary, trusted_text="what shift am I working?")
        with core.use_actor(roster):
            result = core.mcp_server.work_schedule("2025-02-10", "2025-02-10", actor=roster)
        self.assertEqual(result["days"][0]["shift"], "evening")
        spouse = replace(self.actor("spouse-read", "my leave balance", spouse=True), read_scope="family")
        with core.use_actor(spouse):
            self.assertEqual(core.mcp_server.work_leave_balance(actor=spouse)["status"], "not_configured")
        forbidden = replace(owner, trusted_text="what is my wife's leave balance?")
        with core.use_actor(forbidden), self.assertRaises(core.ToolError):
            core.mcp_server.work_leave_balance(actor=forbidden)
        family_query = replace(owner, trusted_text="show the family leave balances", read_scope="family")
        with core.use_actor(family_query), self.assertRaises(core.ToolError):
            core.mcp_server.work_leave_balance(actor=family_query)
        group = replace(roster, conversation_type="GROUP", conversation_id="synthetic@g.us")
        with core.use_actor(group), self.assertRaises(core.ToolError):
            core.mcp_server.work_schedule("2025-02-10", "2025-02-10", actor=group)

    def test_effective_versions_overnight_and_explicit_override(self):
        actor = self.owner()
        self.load(actor)
        newer = profile()
        newer["overtime"]["effective_from"] = "2025-03-01"
        newer["overtime"]["morning_weekend_standard_start"] = "07:00"
        self.load(actor, newer)
        with core.use_actor(actor):
            old = core.phase2_work.default_ot_for_date("2025-02-08", actor.phone)
            current = core.phase2_work.default_ot_for_date("2025-03-08", actor.phone)
            self.assertEqual(old["start"], "06:00")
            self.assertEqual(current["start"], "07:00")
            shift = core.phase2_work.effective_shift("2025-02-10", actor.phone)
            self.assertEqual(shift["end_local"], "2025-02-11T02:00")
            after = core.phase2_work.default_ot_for_date("2025-02-10", actor.phone)
            self.assertEqual(after["start_local"], "2025-02-11T02:00")
            self.assertEqual(after["end_local"], "2025-02-11T05:00")
            weekend = core.phase2_work.default_ot_for_date("2025-02-15", actor.phone)
            self.assertEqual(weekend["end_local"], "2025-02-16T05:00")
            self.assertFalse(core.phase2_work.default_ot_for_date("2025-02-16", actor.phone)["ot"])
            core.phase2_work.record_work_event("OT_PLANNED", "2025-03-08", actor.phone,
                                             start_time="09:00", end_time="13:00")
            self.assertEqual(core.phase2_work.effective_ot_for_date("2025-03-08", actor.phone)["start"], "09:00")

    def test_runtime_import_not_overwritten_by_ha_sync(self):
        actor = self.owner()
        self.load(actor)
        stale = {**profile()["roster"], "owner": "husband", "visibility": "private", "pattern": "off"}
        core.profile_config.sync_options({"roster_profiles": [stale]})
        core.profile_config.sync_options({})
        with core.use_actor(actor):
            self.assertEqual(core.phase2_work.effective_shift("2025-02-03", actor.phone)["shift"], "morning")

    def test_invalid_import_is_atomic_and_nonfinite_rejected(self):
        actor = self.owner()
        invalid = profile()
        invalid["leave"][0]["used_days"] = float("nan")
        with self.assertRaises(ValueError):
            self.load(actor, invalid)
        conn = core.db.connect()
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM alex_profile_config_versions").fetchone()[0], 0)
        finally:
            conn.close()

    def test_leave_snapshot_rejects_impossible_and_future_balance(self):
        actor = self.owner()
        invalid = profile()
        invalid["leave"][0].pop("used_days")
        invalid["leave"][0]["remaining_days"] = 999
        with self.assertRaises(ValueError):
            self.load(actor, invalid)
        self.load(actor)
        with core.use_actor(actor), self.assertRaises(ValueError):
            core.phase2_work.leave_balance("annual", actor.phone, as_of_date="2024-12-31")

    def test_brief_real_context_date_counts_and_private_guard(self):
        actor = self.owner()
        self.load(actor)
        family_context = replace(actor, read_scope="family")
        with core.use_actor(family_context):
            result = daily_brief.build(family_context, "2025-02-13")
        self.assertIn("Thursday, 13 February 2025", result["text"])
        self.assertIn("Afternoon shift", result["text"])
        self.assertIn("of 0 open", result["text"])
        with self.assertRaises(PermissionError):
            daily_brief.build(replace(actor, conversation_type="GROUP"))

    def test_local_midnight_dst_and_due_count_are_grounded(self):
        actor = self.owner()
        core.phase2.create_task(replace(actor, action_key="synthetic-due"), "Due task", due_local="2025-02-13")
        core.phase2.create_task(replace(actor, action_key="synthetic-future"), "Future task", due_local="2025-02-20")
        with patch.dict(os.environ, {"ALEX_CERT_NOW": "2025-02-12T17:00:00+00:00"}):
            result = daily_brief.build(actor)
        self.assertEqual(result["date"], "2025-02-13")
        self.assertEqual(result["counts"]["tasks"], 1)
        self.assertEqual(result["counts"]["open_tasks"], 2)
        with patch.dict(os.environ, {"ALEX_CERT_NOW": "2025-03-09T07:30:00+00:00"}):
            dst = daily_brief.build(replace(actor, timezone="America/New_York"))
        self.assertIn("Sunday, 09 March 2025", dst["text"])

    def test_every_regeneration_acknowledges_same_object_once(self):
        actor = self.owner()
        first = daily_brief.create_or_get(actor, "2025-02-13")
        with patch.object(daily_brief, "build", return_value={"date": "2025-02-13", "text": "Updated", "actionable": True}):
            second = daily_brief.create_or_get(actor, "2025-02-13", refresh=True)
        self.assertEqual(first["brief_id"], second["brief_id"])
        self.assertEqual(second["text"], "Updated")
        for index in (1, 2):
            oid = core.db.queue_outbound(actor.conversation_id, "TEXT", text=str(index),
                                         context_kind="DAILY_BRIEF", context_id=first["brief_id"])
            conn = core.db.connect()
            conn.execute("UPDATE outbound_messages SET provider_message_id=?,delivery_status='SENT' WHERE outbound_id=?", (f"brief-{index}", oid))
            conn.commit(); conn.close()
        spouse = self.actor("spouse-ack", "", spouse=True)
        self.assertEqual(daily_brief.acknowledge_reaction(spouse, "brief-2", "✅")["status"], "not_daily_brief_ack")
        self.assertEqual(daily_brief.acknowledge_reaction(actor, "brief-2", "✅")["status"], "acknowledged")
        self.assertEqual(daily_brief.acknowledge_reaction(actor, "brief-1", "✅")["status"], "already_acknowledged")
        next_day = daily_brief.create_or_get(actor, "2025-02-14")
        self.assertEqual(next_day["status"], "PENDING")
        self.assertNotEqual(next_day["brief_id"], first["brief_id"])

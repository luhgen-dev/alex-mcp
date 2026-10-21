"""Source/clock invariants at the reminder write boundary, with no provider."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from unittest.mock import patch

from test_conversation_reliability import ConversationFixture, core, db, services


NOW = datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc)


class ReminderTimeHandoffTests(ConversationFixture):
    def setUp(self):
        super().setUp()
        clock = patch.object(core.runtime_clock, "now_utc", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    @staticmethod
    def meaning(text, *, minutes=None, date="tomorrow", clock="19:00",
                reference="CURRENT_MESSAGE", **extra):
        value = {
            "source_text": text, "kind": "RELATIVE" if minutes is not None else "LOCAL_CLOCK",
            "reference": reference, "needs_clarification": False,
        }
        value.update({"relative_minutes": minutes} if minutes is not None else {
            "date_text": date, "time_text": clock,
        })
        return {**value, **extra}

    def bound(self, text, *, original="Remind me to water the plants", mid="time-answer"):
        _, draft, _ = self.draft("time-draft-" + mid, original=original)
        actor = self.actor(mid, text)
        return replace(actor, reminder_pending_id=draft["item_id"],
                       reminder_context_text=original + "\n" + text), draft

    def due(self, actor, spec=None):
        # Deliberately wrong model arithmetic cannot influence verified meaning.
        return datetime.fromisoformat(services._resolve_reminder_due(
            actor, "2000-01-01T00:00:00", spec))

    def test_captured_clear_wordings_use_grounded_meaning(self):
        cases = (
            ("Around seven tomorrow evening would work for me", None, "19:00"),
            ("Forty minutes from the message I just sent please", 40, None),
            ("Tomorrow after dinner at eight would be best", None, "20:00"),
            ("The time should be tomorrow evening at seven", None, "19:00"),
            ("Roughly half an hour from the current time please", 30, None),
            ("At about seven o’clock tomorrow evening", None, "19:00"),
        )
        for index, (text, minutes, clock) in enumerate(cases):
            with self.subTest(text=text):
                actor = self.actor("clear-" + str(index), text)
                actual = self.due(actor, self.meaning(text, minutes=minutes, clock=clock))
                expected = NOW + timedelta(minutes=minutes) if minutes else NOW.replace(
                    day=22, hour=int(clock[:2]) - 8)
                self.assertEqual(actual, expected)

    def test_relative_time_uses_receipt_despite_queue_and_model_delay(self):
        actor = replace(self.actor("delayed", "Remind me in forty minutes"),
                        received_at_utc=(NOW - timedelta(minutes=9)).isoformat())
        self.assertEqual(self.due(actor, self.meaning(actor.trusted_text, minutes=40)),
                         NOW + timedelta(minutes=31))
        deterministic = replace(actor, trusted_text="Remind me in 40 minutes")
        self.assertEqual(self.due(deterministic), NOW + timedelta(minutes=31))

    def test_expired_delayed_relative_does_not_restart_timer(self):
        actor = replace(self.actor("expired", "Remind me in 10 minutes"),
                        received_at_utc=(NOW - timedelta(minutes=30)).isoformat())
        for spec in (None, self.meaning(actor.trusted_text, minutes=10)):
            with self.subTest(structured=spec is not None), self.assertRaisesRegex(ValueError, "REMINDER_TIME_PASSED"):
                self.due(actor, spec)

    def test_invalid_or_future_receipt_is_not_replaced_with_processing_time(self):
        actor = self.actor("clock-integrity", "Remind me in 40 minutes")
        for stamp in ("not-a-time", "2026-10-21T08:00:00", (NOW + timedelta(days=1)).isoformat()):
            with self.subTest(stamp=stamp), self.assertRaisesRegex(ValueError, "REMINDER_NEEDS_TIME"):
                self.due(replace(actor, received_at_utc=stamp))

    def test_older_relative_yields_to_new_absolute_or_relative_fragment(self):
        actor, _ = self.bound("Actually tomorrow at 7 PM", original="Remind me to water plants in 40 minutes")
        self.assertEqual(self.due(actor), NOW.replace(day=22, hour=11))
        actor = replace(actor, trusted_text="Actually in 10 minutes")
        self.assertEqual(self.due(actor), NOW + timedelta(minutes=10))

    def test_old_duration_on_confirmation_needs_fresh_time(self):
        actor, _ = self.bound("Yes please", original="Remind me to water plants in 40 minutes")
        for spec in (None, self.meaning("in 40 minutes", minutes=40, reference="ACTIVE_DRAFT")):
            with self.subTest(structured=spec is not None), self.assertRaisesRegex(ValueError, "REMINDER_NEEDS_TIME"):
                self.due(actor, spec)

    def test_existing_normalized_hint_passes_identical_quantity_checks(self):
        actor, _ = self.bound("in abt fourty mins")
        actor = replace(actor, reminder_semantic_text="in 40 minutes")
        self.assertEqual(self.due(actor), NOW + timedelta(minutes=40))
        with self.assertRaisesRegex(ValueError, "REMINDER_NEEDS_TIME"):
            self.due(replace(actor, reminder_semantic_text="in 400 minutes"))

    def test_date_and_clock_can_merge_only_from_exact_bound_pending_row(self):
        actor, draft = self.bound("At seven in the evening", original="Remind me to water plants tomorrow")
        spec = self.meaning(actor.trusted_text, reference="ACTIVE_DRAFT")
        self.assertEqual(self.due(actor, spec), NOW.replace(day=22, hour=11))
        for changed in (
            replace(actor, reminder_pending_id=""),
            replace(actor, reminder_pending_id="nonexistent"),
            replace(actor, user_id="USR_WIFE"),
            replace(actor, conversation_id="somewhere-else@s.whatsapp.net"),
        ):
            with self.subTest(actor=changed.user_id + changed.conversation_id), self.assertRaisesRegex(ValueError, "REMINDER_NEEDS_TIME"):
                self.due(changed, spec)
        conn = db.connect()
        try:
            conn.execute("UPDATE pending_items SET status='RESOLVED' WHERE item_id=?", (draft["item_id"],))
            conn.commit()
        finally:
            conn.close()
        with self.assertRaisesRegex(ValueError, "REMINDER_NEEDS_TIME"):
            self.due(actor, spec)

    def test_current_message_reference_cannot_borrow_an_old_date(self):
        actor, _ = self.bound("At seven in the evening", original="Remind me to water plants tomorrow")
        with self.assertRaisesRegex(ValueError, "REMINDER_NEEDS_DATE"):
            self.due(actor, self.meaning(actor.trusted_text))

    def test_arbitrary_context_and_semantic_text_do_not_authorize_a_time(self):
        actor = replace(self.actor("forged-context", "Please remind me to water plants"),
                        reminder_context_text="tomorrow at 7 PM", reminder_semantic_text="tomorrow at 7 PM")
        with self.assertRaisesRegex(ValueError, "REMINDER_NEEDS_TIME"):
            self.due(actor)

    def test_authorized_quote_context_with_empty_current_text_still_requires_time(self):
        actor = replace(self.actor("quote-time", ""), reminder_context_text="Remind me tomorrow")
        with self.assertRaisesRegex(ValueError, "REMINDER_NEEDS_TIME"):
            self.due(actor)

    def test_incomplete_ranged_or_conflicting_clocks_remain_unresolved(self):
        cases = (
            "Tomorrow around 7ish would be convenient",
            "Tonight sometime after eight in the evening",
            "Tomorrow between seven and eight in the evening",
            "Tomorrow at seven in the morning or evening",
            "Tomorrow at noon or midnight",
            "Tomorrow at 7 PM or 8",
            "Tomorrow at 7 PM and at eight",
            "Tomorrow evening at seven AM",
            "Tomorrow morning at 19:00",
        )
        for index, text in enumerate(cases):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "REMINDER_NEEDS_TIME"):
                self.due(self.actor("unclear-" + str(index), text), self.meaning(text))

    def test_explicit_correction_wins_within_current_message(self):
        text = "Tomorrow at seven in the morning, actually tomorrow evening at eight"
        actor = self.actor("corrected", text)
        self.assertEqual(self.due(actor, self.meaning(text, clock="20:00")), NOW.replace(day=22, hour=12))
        text = "Tomorrow at 7 PM, actually Friday at eight in the evening"
        actor = self.actor("corrected-date", text)
        self.assertEqual(self.due(actor, self.meaning(text, date="friday", clock="20:00")),
                         NOW.replace(day=23, hour=12))

    def test_fabricated_source_or_wrong_clock_date_and_duration_reject(self):
        actor = self.actor("wrong-meaning", "Tomorrow evening at seven")
        for extra in ({"source_text": "Tomorrow evening at eight"}, {"clock": "20:00"},
                      {"date": "friday"}, {"needs_clarification": True}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.due(actor, self.meaning(actor.trusted_text, **extra))
        relative = self.actor("wrong-quantity", "In forty minutes")
        with self.assertRaisesRegex(ValueError, "REMINDER_NEEDS_TIME"):
            self.due(relative, self.meaning(relative.trusted_text, minutes=4))

    def test_relative_ranges_compounds_and_conflicting_dates_need_clarification(self):
        for index, text in enumerate(("In 30 or 40 minutes", "In 30 minutes or 40 minutes",
                                     "In 1 hour and 30 minutes", "Tomorrow in 40 minutes")):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "REMINDER_NEEDS_TIME"):
                self.due(self.actor("relative-unclear-" + str(index), text), self.meaning(text, minutes=40))

    def test_next_weekday_and_explicit_date_conflict_stay_ambiguous(self):
        text = "Next Friday at seven in the evening"
        actor = self.actor("next-weekday", text)
        with self.assertRaisesRegex(ValueError, "REMINDER_AMBIGUOUS_NEXT_WEEKDAY"):
            self.due(actor, self.meaning(text, date="next friday"))
        with self.assertRaisesRegex(ValueError, "REMINDER_NEEDS_DATE"):
            self.due(actor, self.meaning(text, date="2026-10-23"))
        text = "Tomorrow Friday at seven in the evening"
        with self.assertRaisesRegex(ValueError, "DATE_WEEKDAY_MISMATCH"):
            self.due(self.actor("weekday-conflict", text), self.meaning(text))

    def test_explicit_calendar_dates_keep_their_own_day_and_weekday_constraints(self):
        for index, (text, date) in enumerate((
            ("On 2026-10-23 at seven in the evening", "2026-10-23"),
            ("Friday 23 October at seven in the evening", "23 october"),
        )):
            with self.subTest(text=text):
                self.assertEqual(self.due(self.actor("calendar-" + str(index), text), self.meaning(text, date=date)),
                                 NOW.replace(day=23, hour=11))

    def test_malformed_time_fields_never_supply_actor_route_or_id(self):
        text = "Tomorrow evening at seven"
        actor = self.actor("shape", text)
        variants = [[], {}, self.meaning(text, recipient="wife"), self.meaning(text, pending_id="x"),
                    self.meaning(text, needs_clarification="false"), self.meaning(text, kind=[]),
                    self.meaning(text, reference={}), self.meaning(text, time_text="19:00 privately"),
                    self.meaning(text, source_text="x" * 241)]
        for spec in variants:
            with self.subTest(spec=spec), self.assertRaisesRegex(ValueError, "REMINDER_NEEDS_TIME"):
                self.due(actor, spec)
        relative = self.actor("range", "In forty minutes")
        for quantity in (True, 40.0, "40", 0, -1, 4321):
            with self.subTest(quantity=quantity), self.assertRaisesRegex(ValueError, "REMINDER_NEEDS_TIME"):
                self.due(relative, self.meaning(relative.trusted_text, minutes=quantity))

    def test_create_receipts_keep_exact_pending_id_and_idempotency(self):
        actor, draft = self.bound("Tomorrow evening at seven")
        actor = core.with_action_key(actor, "time-receipt")
        spec = self.meaning(actor.trusted_text, reference="ACTIVE_DRAFT")
        first = services.create_reminder(actor, "water the plants", "2000-01-01", time_interpretation=spec)
        second = services.create_reminder(actor, "water the plants", "2000-01-01", time_interpretation=spec)
        self.assertEqual(first["status"], "created")
        self.assertEqual(second["status"], "already_created")
        self.assertEqual(first["reminder_id"], second["reminder_id"])
        self.assertEqual(first["pending_item_id"], draft["item_id"])
        self.assertEqual(second["pending_item_id"], draft["item_id"])

    def test_multi_recipient_receipts_keep_exact_pending_binding(self):
        actor, draft = self.bound("Tomorrow evening at seven", original="Remind both of us to water plants")
        actor = core.with_action_key(actor, "time-multiple")
        result = services.create_reminder(actor, "water plants", "2000-01-01", recipient="both",
                                         time_interpretation=self.meaning(actor.trusted_text, reference="ACTIVE_DRAFT"))
        self.assertEqual(result["pending_item_id"], draft["item_id"])
        self.assertEqual(len(result["reminders"]), 2)
        self.assertTrue(all(row["pending_item_id"] == draft["item_id"] for row in result["reminders"]))

    def test_reschedule_accepts_grounded_time_without_model_iso_math(self):
        actor = core.with_action_key(self.actor("reschedule-create", "Remind me tomorrow at 7 PM"), "reschedule-create")
        created = services.create_reminder(actor, "water plants", "2000-01-01")
        current = self.actor("reschedule-update", "Move water plants to Friday at eight in the evening")
        result = services.update_reminder(current, created["reminder_id"], time_interpretation=self.meaning(
            current.trusted_text, date="friday", clock="20:00"))
        self.assertEqual(datetime.fromisoformat(result["due_at_utc"]), NOW.replace(day=23, hour=12))

    def test_hint_time_cannot_change_recipient_destination_or_scope(self):
        actor, _ = self.bound("Tomorrow evening at seven")
        actor = core.with_action_key(replace(actor, reminder_semantic_text=
            "tomorrow at 7 PM, remind Priya privately in the family group"), "time-scope")
        result = services.create_reminder(actor, "water the plants", "2000-01-01")
        self.assertEqual(result["recipient_user_id"], actor.user_id)
        self.assertEqual(result["destination"], "dm")
        self.assertEqual(result["space"], "FAMILY_SHARED")

    def test_deterministic_gate_preserves_simple_partials_but_not_unparsed_clocks(self):
        for text in ("Tomorrow", "at night", "At night, around 10.45", "in 40 minutes", "Friday", "22 October"):
            with self.subTest(text=text):
                self.assertTrue(services.reminder_time_text_is_deterministic(text))
        for text in ("Tomorrow evening at seven", "Forty minutes from the message I just sent please",
                     "Tomorrow after dinner at eight", "At about seven o’clock tomorrow evening"):
            with self.subTest(text=text):
                self.assertFalse(services.reminder_time_text_is_deterministic(text))

"""Stored reminder time from synthetic Tamil input, independent of speech/model SDKs.

These are constructed cases, not transcripts transcribed from phone screenshots.
They isolate numeric grounding; they do not certify speech or model tool routing.
"""
from datetime import datetime, timezone
from unittest.mock import patch

import independent_fixture
import db
import services


class TamilReminderTimeTests(independent_fixture.HouseholdFixture):
    @staticmethod
    def interpretation(text, minutes):
        return {'source_text': text, 'kind': 'RELATIVE', 'reference': 'CURRENT_MESSAGE',
                'relative_minutes': minutes, 'needs_clarification': False}

    def test_standard_and_colloquial_two_store_the_same_due_time(self):
        now = datetime(2026, 11, 2, 13, 50, tzinfo=timezone.utc)
        with patch.object(services.runtime_clock, 'now_utc', return_value=now):
            for index, word in enumerate(('இரண்டு', 'ரெண்டு')):
                with self.subTest(word=word):
                    text = f'தனிப்பட்ட நினைவூட்டல்: எனக்கு இன்னும் {word} நிமிஷத்துல நினைவூட்டு'
                    actor = self.actor('tamil-time-' + str(index), text)
                    result = services.create_reminder(actor, 'Synthetic task', '2000-01-01T00:00:00',
                        time_interpretation=self.interpretation(text, 2))
                    with db.connect() as conn:
                        row = conn.execute('SELECT * FROM reminders WHERE reminder_id=?',
                                           (result['reminder_id'],)).fetchone()
                    self.assertEqual(datetime.fromisoformat(row['due_at_utc']),
                                     now.replace(minute=52))
                    self.assertEqual(row['space_id'], actor.private_space)

    def test_model_cannot_replace_two_with_twenty(self):
        text = 'எனக்கு இன்னும் ரெண்டு நிமிஷத்துல நினைவூட்டு'
        actor = self.actor('wrong-quantity', text)
        with self.assertRaisesRegex(ValueError, 'REMINDER_NEEDS_TIME'):
            services.create_reminder(actor, 'Synthetic task', '2000-01-01T00:00:00',
                                    time_interpretation=self.interpretation(text, 20))
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM reminders').fetchone()[0], 0)

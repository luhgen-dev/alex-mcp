"""Real HA adapter logic, simulated HTTP/state responses; no physical actions."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'alex-mcp/app'))
import ha


class HAControlReadbackTests(unittest.TestCase):
    def control(self, entity, action, value=None, *, before=None, after=None):
        before = before or {'entity_id': entity, 'state': 'off', 'attributes': {}}
        after = after or {'entity_id': entity, 'state': 'on', 'attributes': {}}
        with patch.object(ha, 'get_state', side_effect=[before] + [after] * 4), \
             patch.object(ha, '_request', return_value=[]) as request, \
             patch.object(ha.time, 'sleep'):
            result = ha.control(entity, action, value)
        self.assertEqual(request.call_count, 1)
        return result, request.call_args.args

    def test_light_on_is_not_proof_of_requested_brightness(self):
        for attrs in ({}, {'brightness': 255}, {'brightness': 'invalid'}, {'brightness': float('nan')}):
            with self.subTest(attrs=attrs):
                result, call = self.control('light.room', 'turn_on', 50,
                    after={'entity_id': 'light.room', 'state': 'on', 'attributes': attrs})
                self.assertEqual(result['status'], 'requested_unconfirmed')
                self.assertEqual(call[2], {'entity_id': 'light.room', 'brightness_pct': 50})

    def test_light_brightness_requires_matching_readback_and_zero_means_off(self):
        result, _ = self.control('light.room', 'turn_on', 50,
            after={'state': 'on', 'attributes': {'brightness': 128}})
        self.assertEqual(result['status'], 'executed_and_verified')
        result, _ = self.control('light.room', 'turn_on', 0,
            after={'state': 'off', 'attributes': {}})
        self.assertEqual(result['status'], 'executed_and_verified')

    def test_invalid_numeric_values_never_read_or_control_home_assistant(self):
        cases = [('light.room', 'turn_on', x) for x in (-1, 101)]
        cases += [('fan.room', 'set_percentage', x) for x in (-1, 101)]
        cases += [('media_player.room', 'volume_set', x) for x in (-0.1, 1.1)]
        cases += [(entity, action, value)
                  for entity, action in [('light.room', 'turn_on'), ('fan.room', 'set_percentage'),
                                         ('climate.room', 'set_temperature'), ('media_player.room', 'volume_set')]
                  for value in (float('nan'), float('inf'), float('-inf'), True)]
        cases += [('switch.room', 'turn_on', 50)]
        for entity, action, value in cases:
            with self.subTest(entity=entity, action=action, value=value), \
                 patch.object(ha, 'get_state') as get, patch.object(ha, '_request') as request:
                with self.assertRaises(ValueError):
                    ha.control(entity, action, value)
                get.assert_not_called()
                request.assert_not_called()

    def test_numeric_controls_verify_target_attribute_not_service_acceptance(self):
        cases = [('fan.room', 'set_percentage', 50, 'percentage', 50, 20),
                 ('climate.room', 'set_temperature', 24, 'temperature', 24, 28),
                 ('media_player.room', 'volume_set', 0.4, 'volume_level', 0.4, 0.8)]
        for entity, action, value, attr, correct, wrong in cases:
            for observed, expected in [(correct, 'executed_and_verified'), (wrong, 'requested_unconfirmed')]:
                with self.subTest(action=action, observed=observed):
                    result, _ = self.control(entity, action, value,
                        after={'state': 'on', 'attributes': {attr: observed}})
                    self.assertEqual(result['status'], expected)

    def test_climate_and_media_on_states_are_domain_specific(self):
        for entity, active in [('climate.room', 'cool'), ('media_player.room', 'playing')]:
            with self.subTest(entity=entity):
                result, _ = self.control(entity, 'turn_on', after={'state': active, 'attributes': {}})
                self.assertEqual(result['status'], 'executed_and_verified')
                result, _ = self.control(entity, 'turn_on', after={'state': 'off', 'attributes': {}})
                self.assertEqual(result['status'], 'requested_unconfirmed')

    def test_toggle_does_not_accept_an_arbitrary_changed_state(self):
        result, _ = self.control('light.room', 'toggle',
            before={'state': 'off'}, after={'state': 'unrelated'})
        self.assertEqual(result['status'], 'requested_unconfirmed')
        result, _ = self.control('light.room', 'toggle',
            before={'state': 'off'}, after={'state': 'on'})
        self.assertEqual(result['status'], 'executed_and_verified')


if __name__ == '__main__':
    unittest.main()

import importlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        import app
        self.app = importlib.reload(app)
        self.app.STATE_FILE = Path(self.tmp.name) / 'state.json'
        self.client = TestClient(self.app.app)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_unconfigured_integrations_make_no_network_requests(self):
        with patch('urllib.request.urlopen', side_effect=AssertionError('unexpected network')) as network:
            response = self.client.get('/api/state')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['usage'], {})
            self.assertTrue(all(not value for value in self.app.ENTITIES.values()))
            self.client.get('/api/weather')
            self.assertEqual(self.client.post('/api/room/toggle').status_code, 409)
            self.assertEqual(self.client.post('/api/morning').status_code, 409)
            self.assertEqual(self.client.post('/api/night').status_code, 409)
            network.assert_not_called()

    def test_static_assets(self):
        for route in ['/', '/manifest.webmanifest', '/icon.svg', '/icon-192.png', '/icon-512.png', '/fredoka-bold.ttf', '/oxanium-clear-zero.ttf', '/oswald-clock.ttf']:
            self.assertEqual(self.client.get(route).status_code, 200, route)
        self.assertEqual(self.client.get('/').headers.get('cache-control'), 'no-store')
        manifest = self.client.get('/manifest.webmanifest').json()
        self.assertEqual(manifest['display'], 'fullscreen')

    def test_daily_reset(self):
        result = self.app.reset_daily({'date': '2000-01-01', 'morning_done': True, 'night_done': True})
        self.assertFalse(result['morning_done'])
        self.assertFalse(result['night_done'])
        self.assertEqual(result['date'], self.app.today())

    def test_state_exposes_reusable_flow_configuration(self):
        self.app.USER_NAME = 'Umair'
        self.app.ENTITIES['shuruq'] = 'sensor.shuruq'
        self.app.ENTITIES['isha_iqama'] = 'sensor.isha_iqama'
        with patch.object(self.app, 'state', side_effect=lambda entity: {'entity_id': entity, 'state': '06:30'}):
            body = self.client.get('/api/state').json()
        self.assertEqual(body['user_name'], 'Umair')
        self.assertEqual(body['shuruq']['entity_id'], 'sensor.shuruq')
        self.assertEqual(body['isha_iqama']['entity_id'], 'sensor.isha_iqama')

    def test_successful_routine_is_idempotent_for_the_day(self):
        self.app.MORNING_AUTOMATION = 'automation.morning'
        with patch.object(self.app, 'ha', return_value={}) as ha:
            first = self.client.post('/api/morning')
            second = self.client.post('/api/morning')
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()['already_done'])
        self.assertTrue(second.json()['already_done'])
        ha.assert_called_once()

    def test_morning_persists_weather_deadline_and_state_has_server_clock(self):
        self.app.MORNING_AUTOMATION = 'automation.morning'
        before = time.time()
        with patch.object(self.app, 'ha', return_value={}):
            response = self.client.post('/api/morning').json()
        saved = self.app.load_state()
        self.assertGreaterEqual(saved['morning_weather_until'], before + 119)
        self.assertEqual(response['morning_weather_until'], saved['morning_weather_until'])
        state = self.client.get('/api/state').json()
        self.assertAlmostEqual(state['server_now'], time.time(), delta=2)
        self.assertEqual(state['morning_weather_until'], saved['morning_weather_until'])

    def test_save_state_is_atomic_and_leaves_no_temporary_file(self):
        state = {'date': self.app.today(), 'morning_done': True, 'night_done': False,
                 'morning_weather_until': 123.0}
        self.app.save_state(state)
        self.assertEqual(self.app.load_state(), state)
        self.assertFalse(self.app.STATE_FILE.with_suffix('.tmp').exists())

    def test_failed_routine_is_not_marked_complete(self):
        self.app.NIGHT_AUTOMATION = 'automation.night'
        with patch.object(self.app, 'ha', side_effect=RuntimeError('HA offline')):
            response = self.client.post('/api/night')
        self.assertEqual(response.status_code, 503)
        self.assertFalse(self.app.load_state()['night_done'])

    def test_ai_data_is_fetched_directly_from_monitor(self):
        self.app.AI_URL = 'http://localhost:8765'
        with patch('urllib.request.urlopen') as get:
            get.return_value.__enter__.return_value.read.return_value = b'{"codex": {}, "claude": {}}'
            self.assertEqual(self.app.ai_usage(), {'codex': {}, 'claude': {}})
            self.assertEqual(get.call_args.args[0], 'http://localhost:8765/api/usage')

    def test_upstream_errors_do_not_expose_details(self):
        with patch.object(self.app, 'ai_usage', side_effect=RuntimeError('private-provider-detail')):
            response = self.client.get('/api/state')
            self.assertNotIn('private-provider-detail', response.text)


if __name__ == '__main__':
    unittest.main()

import importlib
import os
import tempfile
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

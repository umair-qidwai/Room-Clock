import importlib
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import unittest
from urllib.error import HTTPError
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient


def frozen_datetime(instant):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz)
    return FrozenDateTime


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

    def test_today_uses_configured_local_timezone(self):
        self.app.TIMEZONE = ZoneInfo('Pacific/Kiritimati')
        fake_datetime = frozen_datetime(datetime(2026, 10, 8, 22, 40, tzinfo=timezone.utc))
        with patch.object(self.app, 'datetime', fake_datetime):
            self.assertEqual(self.app.today(), '2026-10-09')

    def test_get_waits_for_post_state_transaction(self):
        self.app.MORNING_AUTOMATION = 'automation.morning'
        entered, release, get_loaded = threading.Event(), threading.Event(), threading.Event()
        original_load = self.app.load_state

        def load():
            if entered.is_set():
                get_loaded.set()
            return original_load()

        def trigger(*args):
            entered.set()
            self.assertTrue(release.wait(2))
            return {}

        with patch.object(self.app, 'ha', side_effect=trigger), patch.object(self.app, 'load_state', side_effect=load):
            with ThreadPoolExecutor(max_workers=2) as pool:
                post = pool.submit(self.client.post, '/api/morning')
                self.assertTrue(entered.wait(2))
                get = pool.submit(self.client.get, '/api/state')
                try:
                    self.assertFalse(get_loaded.wait(0.15), 'GET read state during POST transaction')
                finally:
                    release.set()
                self.assertEqual(post.result().status_code, 200)
                self.assertTrue(get.result().json()['morning_done'])
        self.assertTrue(self.app.load_state()['morning_done'])

    def test_corrupt_state_is_reported_without_overwriting_history(self):
        self.app.MORNING_AUTOMATION = 'automation.morning'
        for contents in ['{broken', '[]', '{}', '{"date":"2026-10-14","morning_done":"false","night_done":false}', '{"date":"2026-10-14","morning_done":true,"night_done":false,"morning_weather_until":NaN}']:
            with self.subTest(contents=contents):
                self.app.STATE_FILE.write_text(contents)
                with patch.object(self.app, 'ha') as ha, self.assertLogs('room-clock', level='ERROR'):
                    self.assertEqual(self.client.get('/api/state').status_code, 503)
                    self.assertEqual(self.client.post('/api/morning').status_code, 503)
                self.assertEqual(self.app.STATE_FILE.read_text(), contents)
                ha.assert_not_called()

    def test_schedule_times_use_backend_calendar_and_preserve_timestamps(self):
        self.app.TIMEZONE = ZoneInfo('America/New_York')
        instant = datetime(2026, 10, 8, 2, 40, tzinfo=timezone.utc)
        self.app.ENTITIES['shuruq'] = 'sensor.shuruq'
        self.app.ENTITIES['isha_iqama'] = 'sensor.isha'
        timestamps = ['2026-10-09T06:30:00+00:00', '2026-10-09T06:30:00Z', 'unavailable']
        with patch.object(self.app, 'datetime', frozen_datetime(instant)):
            for raw in ['6:30', '06:30:12', *timestamps]:
                with self.subTest(raw=raw), patch.object(self.app, 'state', return_value={'state': raw, 'attributes': {'unit': 'kept'}}):
                    body = self.client.get('/api/state').json()
                self.assertEqual(body['date'], '2026-10-07')
                self.assertEqual(body['night_date'], '2026-10-07')
                self.assertEqual(body['timezone'], 'America/New_York')
                expected = {'6:30': '2026-10-07T06:30:00-04:00', '06:30:12': '2026-10-07T06:30:12-04:00'}.get(raw, raw)
                for key in ('shuruq', 'isha_iqama'):
                    self.assertEqual(body[key]['state'], expected)
                    self.assertEqual(body[key]['attributes'], {'unit': 'kept'})
                self.assertEqual(body['room']['state'], raw)

    def test_toggle_returns_acceptance_without_a_post_acceptance_read(self):
        self.app.ENTITIES['room_switch'] = 'switch.room'
        current = {'entity_id': 'switch.room', 'state': 'on'}
        with patch.object(self.app, 'state', side_effect=[current, RuntimeError('read unavailable')]) as state, patch.object(self.app, 'ha', return_value={}) as ha:
            response = self.client.post('/api/room/toggle')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'accepted': True, 'requested_state': 'off', 'state': current})
        state.assert_called_once_with('switch.room')
        ha.assert_called_once_with('/api/services/switch/turn_off', 'POST', {'entity_id': 'switch.room'})

    def test_schedule_normalization_retains_non_object_integration_values(self):
        with patch.object(self.app, 'state', return_value=None):
            response = self.client.get('/api/state')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['shuruq'])

    def test_daily_reset(self):
        result = self.app.reset_daily({'date': '2000-01-01', 'morning_done': True, 'night_done': True})
        self.assertFalse(result['morning_done'])
        self.assertFalse(result['night_done'])
        self.assertEqual(result['date'], self.app.today())

    def test_late_night_run_does_not_complete_following_evening(self):
        self.app.NIGHT_AUTOMATION = 'automation.night'
        def at(hour):
            return frozen_datetime(datetime(2026, 10, 11, hour, 30, tzinfo=self.app.TIMEZONE))
        with patch.object(self.app, 'datetime', at(6)), patch.object(self.app, 'ha', return_value={}):
            self.assertEqual(self.client.post('/api/night').status_code, 200)
            self.assertTrue(self.app.reset_daily(self.app.load_state())['night_done'])
        with patch.object(self.app, 'datetime', at(13)):
            self.assertFalse(self.app.reset_daily(self.app.load_state())['night_done'])

    def test_completed_night_stays_complete_across_midnight(self):
        self.app.NIGHT_AUTOMATION = 'automation.night'
        def at(day, hour):
            return frozen_datetime(datetime(2026, 10, day, hour, 30, tzinfo=self.app.TIMEZONE))
        with patch.object(self.app, 'datetime', at(10, 22)), patch.object(self.app, 'ha', return_value={}):
            self.client.post('/api/night')
        with patch.object(self.app, 'datetime', at(11, 1)), patch.object(self.app, 'ha') as ha:
            result = self.client.post('/api/night').json()
            self.assertTrue(result['already_done'])
            ha.assert_not_called()
        with patch.object(self.app, 'datetime', at(11, 12)):
            self.assertFalse(self.app.reset_daily(self.app.load_state())['night_done'])

    def assert_legacy_night_migration(self, marker):
        self.app.TIMEZONE = ZoneInfo('America/New_York')
        self.app.NIGHT_AUTOMATION = 'automation.night'
        for hour in (1, 11, 12, 22):
            for legacy_day in (12, 13, 14):
                with self.subTest(marker=marker, hour=hour, legacy_day=legacy_day):
                    instant = datetime(2026, 10, 14, hour, 30, tzinfo=self.app.TIMEZONE)
                    legacy_date = f'2026-10-{legacy_day:02}'
                    cycle = '2026-10-13' if hour < 12 else '2026-10-14'
                    same_cycle = legacy_day == 14 or (hour < 12 and legacy_day == 13)
                    self.app.save_state({'date': legacy_date, 'morning_done': True,
                                         'night_done': marker == 'night_done',
                                         'night_inflight': marker == 'night_inflight'})
                    with patch.object(self.app, 'datetime', frozen_datetime(instant)), patch.object(self.app, 'ha', return_value={}) as ha:
                        state = self.client.get('/api/state').json()
                        self.assertEqual(state['date'], '2026-10-14')
                        self.assertEqual(state['night_date'], cycle)
                        self.assertEqual(state['morning_done'], legacy_day == 14)
                        self.assertEqual(self.app.load_state()[marker], same_cycle)
                        for attempt in range(2):
                            response = self.client.post('/api/night')
                            if same_cycle and marker == 'night_inflight':
                                self.assertEqual(response.status_code, 503)
                                self.assertIn('unknown', response.json()['detail'])
                            else:
                                self.assertEqual(response.status_code, 200)
                                self.assertEqual(response.json()['already_done'], same_cycle or attempt == 1)
                        if same_cycle:
                            ha.assert_not_called()
                        else:
                            ha.assert_called_once()

    def test_legacy_completed_night_migrates_only_current_cycle_without_retrigger(self):
        self.assert_legacy_night_migration('night_done')

    def test_legacy_unknown_night_migrates_only_current_cycle_without_retrigger(self):
        self.assert_legacy_night_migration('night_inflight')

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

    def test_server_clock_is_sampled_after_delayed_integration(self):
        clock = [1000.0]

        def delayed_usage():
            # Model six seconds of integration latency without sleeping.
            clock[0] += 6
            return {'codex': {}}

        with patch.object(self.app, 'time', SimpleNamespace(time=lambda: clock[0])), patch.object(self.app, 'ai_usage', side_effect=delayed_usage):
            response = self.client.get('/api/state')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['usage'], {'codex': {}})
        self.assertEqual(response.json()['server_now'], 1006.0)

    def test_save_state_is_atomic_and_leaves_no_temporary_file(self):
        state = {'date': self.app.today(), 'morning_done': True, 'night_done': False,
                 'morning_weather_until': 123.0}
        self.app.save_state(state)
        self.assertEqual(self.app.load_state(), state)
        self.assertFalse(self.app.STATE_FILE.with_suffix('.tmp').exists())

    def test_timeout_preserves_unknown_marker_without_retrying(self):
        self.app.NIGHT_AUTOMATION = 'automation.night'
        with patch.object(self.app, 'ha', side_effect=TimeoutError('response lost')) as ha, self.assertLogs('room-clock'):
            first = self.client.post('/api/night')
            second = self.client.post('/api/night')
        self.assertEqual(first.status_code, 503)
        self.assertEqual(second.status_code, 503)
        self.assertIn('unknown', first.json()['detail'].lower())
        self.assertIn('unknown', second.json()['detail'].lower())
        saved = self.app.load_state()
        self.assertTrue(saved['night_inflight'])
        self.assertFalse(saved['night_done'])
        ha.assert_called_once()

    def test_definite_rejection_allows_a_later_explicit_attempt(self):
        self.app.NIGHT_AUTOMATION = 'automation.night'
        self.app.HASS_URL, self.app.HASS_TOKEN = 'http://ha.invalid', 'test-only'
        rejection = HTTPError('http://ha.invalid', 401, 'rejected', {}, None)
        with patch('urllib.request.urlopen', side_effect=rejection), self.assertLogs('room-clock'):
            response = self.client.post('/api/night')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('unknown', response.json()['detail'].lower())
        self.assertFalse(self.app.load_state()['night_inflight'])
        with patch.object(self.app, 'ha', return_value={}) as ha:
            self.assertEqual(self.client.post('/api/night').status_code, 200)
            ha.assert_called_once()

    def test_missing_credentials_are_definite_not_unknown(self):
        self.app.NIGHT_AUTOMATION = 'automation.night'
        with patch('urllib.request.urlopen') as network, self.assertLogs('room-clock'):
            response = self.client.post('/api/night')
        self.assertEqual(response.status_code, 503)
        self.assertFalse(self.app.load_state()['night_inflight'])
        network.assert_not_called()

    def test_persistence_failure_blocks_trigger_or_preserves_unknown_outcome(self):
        self.app.MORNING_AUTOMATION = 'automation.morning'
        original_replace = self.app.os.replace
        for fail_on in (1, 2):
            with self.subTest(fail_on=fail_on):
                self.app.STATE_FILE.unlink(missing_ok=True)
                calls = 0
                def replace(source, target):
                    nonlocal calls
                    calls += 1
                    if calls == fail_on:
                        raise OSError('disk unavailable')
                    return original_replace(source, target)
                with patch.object(self.app.os, 'replace', side_effect=replace), patch.object(self.app, 'ha', return_value={}) as ha, self.assertLogs('room-clock'):
                    response = self.client.post('/api/morning')
                self.assertEqual(response.status_code, 503)
                self.assertEqual(ha.call_count, fail_on - 1)
                if fail_on == 2:
                    self.assertIn('unknown', response.json()['detail'].lower())
                    self.assertTrue(self.app.load_state()['morning_inflight'])
                    with patch.object(self.app, 'ha') as retry:
                        self.assertEqual(self.client.post('/api/morning').status_code, 503)
                        retry.assert_not_called()

    def test_interrupted_marker_stays_unknown_across_midnight_until_cycle_reset(self):
        self.app.NIGHT_AUTOMATION = 'automation.night'
        evening = datetime(2026, 10, 10, 22, tzinfo=self.app.TIMEZONE)
        with patch.object(self.app, 'datetime', frozen_datetime(evening)):
            state = self.app.fresh_state()
            state['night_inflight'] = True
            self.app.save_state(state)
        for hour in (1, 11):
            instant = datetime(2026, 10, 11, hour, tzinfo=self.app.TIMEZONE)
            with patch.object(self.app, 'datetime', frozen_datetime(instant)), patch.object(self.app, 'ha') as ha:
                response = self.client.post('/api/night')
                self.assertEqual(response.status_code, 503)
                self.assertIn('unknown', response.json()['detail'])
                self.assertFalse(self.client.get('/api/state').json()['night_done'])
                ha.assert_not_called()
        with patch.object(self.app, 'datetime', frozen_datetime(datetime(2026, 10, 11, 12, tzinfo=self.app.TIMEZONE))), patch.object(self.app, 'ha', return_value={}) as ha:
            self.assertEqual(self.client.post('/api/night').status_code, 200)
            ha.assert_called_once()

    def test_uncertain_http_or_invalid_response_never_allows_duplicate_trigger(self):
        self.app.NIGHT_AUTOMATION = 'automation.night'
        self.app.HASS_URL, self.app.HASS_TOKEN = 'http://ha.invalid', 'test-only'
        for code in (408, 500, 504, None):
            with self.subTest(code=code):
                self.app.STATE_FILE.unlink(missing_ok=True)
                with patch('urllib.request.urlopen') as network, self.assertLogs('room-clock'):
                    if code is None:
                        network.return_value.__enter__.return_value.read.return_value = b'invalid after acceptance'
                    else:
                        network.side_effect = HTTPError('http://ha.invalid', code, 'uncertain', {}, None)
                    first = self.client.post('/api/night')
                    second = self.client.post('/api/night')
                self.assertEqual(first.status_code, 503)
                self.assertEqual(second.status_code, 503)
                self.assertTrue(self.app.load_state()['night_inflight'])
                self.assertFalse(self.app.load_state()['night_done'])
                network.assert_called_once()

    def test_parallel_get_rollovers_and_post_preserve_completion(self):
        self.app.MORNING_AUTOMATION = 'automation.morning'
        self.app.save_state({'date': '2000-01-01', 'night_date': '2000-01-01', 'morning_done': True, 'night_done': True})
        barrier = threading.Barrier(8)
        def request(index):
            barrier.wait(timeout=2)
            route = '/api/morning' if index % 2 else '/api/state'
            return self.client.post(route) if index % 2 else self.client.get(route)
        with patch.object(self.app, 'ha', return_value={}) as ha, ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(request, range(8)))
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertTrue(self.client.get('/api/state').json()['morning_done'])
        ha.assert_called_once()
        self.assertFalse(self.app.STATE_FILE.with_suffix('.tmp').exists())

    def test_failed_routine_is_not_marked_complete(self):
        self.app.NIGHT_AUTOMATION = 'automation.night'
        with patch.object(self.app, 'ha', side_effect=RuntimeError('HA offline')), self.assertLogs('room-clock'):
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

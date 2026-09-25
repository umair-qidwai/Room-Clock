"""Optional real Chromium regressions; all requests are fulfilled locally.
Run: python -m unittest discover -s tests -p test_browser.py -v
Install playwright and Chromium; CHROMIUM_PATH overrides /usr/bin/chromium.
"""
import json
import os
from pathlib import Path
import unittest
from urllib.parse import urlparse

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(sync_playwright is None, "optional playwright not installed")
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            executable_path=os.environ.get("CHROMIUM_PATH", "/usr/bin/chromium"),
            headless=True, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 800, "height": 360})
        self.page = self.context.new_page()
        self.posts = []
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.state = {
            "room": {"state": "off"},
            "fajr": {"state": "2020-01-01T05:00:00Z"},
            "isha": {"state": "2020-01-01T20:00:00Z"},
            "morning_done": False, "night_done": False,
            "morning_automation_configured": True,
            "night_automation_configured": True,
            "weather": {"state": "clear", "attributes": {"temperature": 72, "temperature_unit": "°F"}},
            "usage": {"codex": {"five_hour_remaining": 100, "weekly_remaining": 78},
                      "claude": {"five_hour_remaining": 64, "weekly_remaining": 92}}}
        self.context.route("**/*", self.route)
        self.page.goto("http://room-clock.test/")
        self.page.evaluate("document.fonts.ready")
        self.page.wait_for_timeout(100)

    def route(self, route):
        path = urlparse(route.request.url).path
        if route.request.method == "POST":
            self.posts.append(path)
            route.fulfill(json={"ok": True})
        elif path == "/api/state":
            route.fulfill(json=self.state)
        elif path == "/api/weather":
            route.fulfill(json={"forecast": []})
        elif path in ("/", "/fredoka-bold.ttf", "/manifest.webmanifest", "/icon.svg"):
            file = ROOT / "static" / ("index.html" if path == "/" else path[1:])
            route.fulfill(path=str(file))
        else:
            route.abort()

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.context.close()

    def pointer(self, selector, kind, y):
        self.page.locator(selector).dispatch_event(kind, {
            "pointerId": 7, "pointerType": "touch", "isPrimary": True,
            "button": 0, "clientX": 140, "clientY": y})

    def swipe(self, selector, dy=-100, slow=False, cancel=False, move=True):
        self.pointer(selector, "pointerdown", 180)
        if move:
            self.pointer(selector, "pointermove", 180 + dy)
        if slow:
            self.page.wait_for_timeout(650)
        self.pointer(selector, "pointercancel" if cancel else "pointerup", 180 + dy)
        self.page.wait_for_timeout(320)

    def active_page(self):
        return self.page.locator('.page:not([inert])').get_attribute('data-page')

    def go_to_page(self, name):
        if self.active_page() != name:
            self.swipe('.page:not([inert]) .clock-pane', -150)
        self.assertEqual(self.active_page(), name)

    def test_two_pages_default_and_bidirectional_drag(self):
        self.assertEqual(self.page.locator('.page').count(), 2)
        self.assertEqual(self.active_page(), 'clock')
        self.pointer('.page:not([inert]) .clock-pane', 'pointerdown', 240)
        self.pointer('.page:not([inert]) .clock-pane', 'pointermove', 100)
        self.assertTrue(self.page.locator('.page[data-page="widgets"]').is_visible())
        transform = self.page.locator('.page.clock-only').evaluate('e=>getComputedStyle(e).transform')
        self.assertIn('-140', transform)
        self.pointer('.page.clock-only .clock-pane', 'pointerup', 100)
        self.page.wait_for_timeout(320)
        self.assertEqual(self.active_page(), 'widgets')
        self.assertEqual(self.page.locator('.page.clock-only .widgets').count(), 0)
        self.assertEqual(self.page.locator('.page.clock-only .date').count(), 1)
        self.swipe('.page:not([inert]) .clock-pane', 100)
        self.assertEqual(self.active_page(), 'clock')
        self.assertEqual(self.posts, [])

    def test_page_cancel_reversal_flick_and_repeated_swipes(self):
        clock = '.page:not([inert]) .clock-pane'
        self.swipe(clock, -15, slow=True)
        self.assertEqual(self.active_page(), 'clock')
        self.swipe(clock, -150, cancel=True)
        self.assertEqual(self.active_page(), 'clock')
        self.pointer(clock, 'pointerdown', 180)
        self.pointer(clock, 'pointermove', 60)
        self.page.wait_for_timeout(650)
        self.pointer(clock, 'pointerup', 178)
        self.page.wait_for_timeout(320)
        self.assertEqual(self.active_page(), 'clock')
        for dy in (-25, -25, 25, 25):
            before = self.active_page()
            self.swipe(clock, dy, move=False)
            self.assertNotEqual(self.active_page(), before)
            self.assertEqual(self.page.locator('.page:visible').count(), 1)
        self.assertEqual(self.posts, [])

    def test_touch_event_fallback_loops_pages(self):
        def touch(kind, y):
            self.page.evaluate('''([kind,y]) => {
                const target=document.querySelector('.page:not([inert]) .clock-pane');
                const touch=new Touch({identifier:9,target,clientX:140,clientY:y});
                const active=kind==='touchend'?[]:[touch];
                target.dispatchEvent(new TouchEvent(kind,{bubbles:true,cancelable:true,
                    touches:active,targetTouches:active,changedTouches:[touch]}));
            }''', [kind, y])
        for expected, start, end in [('widgets', 240, 80), ('clock', 80, 240),
                                     ('widgets', 240, 80), ('clock', 80, 240)]:
            touch('touchstart', start)
            touch('touchmove', end)
            touch('touchend', end)
            self.page.wait_for_timeout(320)
            self.assertEqual(self.active_page(), expected)
        self.assertEqual(self.posts, [])

    def test_widget_gesture_and_wheel_are_independent(self):
        self.go_to_page('widgets')
        self.swipe('#rail', -100)
        self.assertIn('Good morning', self.page.locator('#rail').inner_text())
        self.assertEqual(self.active_page(), 'widgets')
        self.swipe('#rail', 100)
        self.assertIn('AI usage', self.page.locator('#rail').inner_text())
        self.swipe('#rail', -15, slow=True)
        self.assertIn('AI usage', self.page.locator('#rail').inner_text())
        self.swipe('#rail', -100, cancel=True)
        self.assertIn('AI usage', self.page.locator('#rail').inner_text())
        self.swipe('#rail', -25, move=False)
        self.assertIn('Good morning', self.page.locator('#rail').inner_text())
        self.page.locator('.page:not([inert]) .clock-pane').dispatch_event('wheel', {'deltaY': 100})
        self.page.wait_for_timeout(400)
        self.assertIn('Good morning', self.page.locator('#rail').inner_text())
        self.assertEqual(self.active_page(), 'widgets')
        self.assertEqual(self.page.locator('#rail .card').count(), 1)
        self.assertEqual(self.posts, [])

    def test_default_ai_no_prayer_takeover_and_idle_during_drag(self):
        self.go_to_page('widgets')
        self.assertIn('AI usage', self.page.locator('#rail').inner_text())
        self.state['isha']['state'] = '2099-01-01T20:00:00Z'
        self.page.evaluate('refresh()')
        self.assertIn('AI usage', self.page.locator('#rail').inner_text())
        self.swipe('#rail', -100)
        self.page.evaluate('manualUntil=Date.now()-1')
        self.pointer('#rail', 'pointerdown', 180)
        self.pointer('#rail', 'pointermove', 100)
        self.page.wait_for_timeout(1200)
        self.assertEqual(self.page.locator('#rail .card').count(), 2)
        self.pointer('#rail', 'pointercancel', 100)
        self.page.wait_for_timeout(1500)
        self.assertIn('AI usage', self.page.locator('#rail').inner_text())
        self.assertEqual(self.page.locator('#rail .card').count(), 1)
        self.assertEqual(self.posts, [])

    def test_routine_taps_and_swipes_starting_on_buttons(self):
        self.go_to_page('widgets')
        # Real browser mouse events verify delayed pointer capture preserves taps.
        self.page.evaluate('selectWidget(1,0,false)')
        self.page.locator('[data-action="morning"]').click()
        self.assertEqual(self.posts, ['/api/morning'])
        self.page.evaluate('selectWidget(2,0,false)')
        self.page.locator('[data-action="night"]').click()
        self.assertEqual(self.posts, ['/api/morning', '/api/night'])
        self.page.evaluate('selectWidget(1,0,false)')
        box = self.page.locator('[data-action="morning"]').bounding_box()
        x, y = box['x'] + box['width']/2, box['y'] + box['height']/2
        self.page.mouse.move(x, y)
        self.page.mouse.down()
        self.page.mouse.move(x, y-100, steps=8)
        self.page.mouse.up()
        self.page.wait_for_timeout(350)
        self.assertIn('Good night', self.page.locator('#rail').inner_text())
        self.assertEqual(self.posts, ['/api/morning', '/api/night'])
        self.assertEqual(self.active_page(), 'widgets')

    def test_layout_clock_fitting_and_screenshots(self):
        output = Path(os.environ.get('ROOM_CLOCK_SCREENSHOTS', '/tmp/room-clock-preview'))
        output.mkdir(parents=True, exist_ok=True)
        self.page.evaluate('clock=()=>{}')
        for width, height in [(800, 360), (390, 844), (1440, 900)]:
            self.page.set_viewport_size({'width': width, 'height': height})
            for name in ['widgets', 'clock']:
                if self.active_page() != name:
                    self.swipe('.page:not([inert]) .clock-pane', -max(100, int(height * .3)))
                for value in ['1:11', '12:58', '8:08']:
                    self.page.evaluate('(value)=>paintTime(value,null,false)', value)
                    self.page.wait_for_timeout(100)
                    bounds = self.page.evaluate('''() => {
                        const pane=document.querySelector('.page:not([inert]) .clock-pane');
                        const face=pane.querySelector('.time-face'), time=face.parentElement;
                        const faceRect=face.getBoundingClientRect();
                        const rect=pane.getBoundingClientRect(), date=pane.querySelector('.date')?.getBoundingClientRect();
                        return {left:faceRect.left-rect.left,right:rect.right-faceRect.right,
                                bottom:rect.bottom-faceRect.bottom,top:faceRect.top-(date?.bottom||rect.top),
                                width:faceRect.width,height:faceRect.height,paneWidth:rect.width,paneHeight:rect.height,
                                transform:getComputedStyle(face).transform,
                                font:parseFloat(getComputedStyle(time).fontSize)};
                    }''')
                    if name == 'widgets':
                        for edge in ['left', 'right', 'bottom', 'top']:
                            self.assertGreaterEqual(bounds[edge], -1, (width, height, name, value, bounds))
                    if name == 'clock':
                        self.assertEqual(bounds['transform'], 'none',
                                         (width, height, name, value, bounds))
                        fit = self.page.evaluate('''value => {
                            const pane=document.querySelector('.page.clock-only .clock-pane');
                            const date=pane.querySelector('.date').getBoundingClientRect();
                            const metrics=measureClockInk(value);
                            return Math.min(1200,(pane.clientWidth-128)*100/metrics.width,
                                (pane.clientHeight-date.height-12)*100/metrics.height);
                        }''', value)
                        self.assertAlmostEqual(bounds['font'], fit, delta=1,
                                               msg=(width, height, name, value, bounds, fit))
                        if (width, height) == (800, 360):
                            self.assertGreater(bounds['font'], 300,
                                               (width, height, name, value, bounds))
                            ink_right, light_left = self.page.evaluate('''value => {
                                const pane=document.querySelector('.page.clock-only .clock-pane').getBoundingClientRect();
                                const light=document.querySelector('#light').getBoundingClientRect();
                                const font=parseFloat(getComputedStyle(document.querySelector('.page.clock-only .time')).fontSize);
                                const inkWidth=measureClockInk(value).width*font/100;
                                return [pane.left+pane.width/2+inkWidth/2,light.left];
                            }''', value)
                            self.assertLessEqual(ink_right, light_left,
                                                 (value, ink_right, light_left))
                overflow = self.page.evaluate('''() => [...document.querySelectorAll('.page:not([inert]) .card,.page:not([inert]) .rings')].filter(e=>e.scrollWidth>e.clientWidth+1||e.scrollHeight>e.clientHeight+1).map(e=>({className:e.className,clientWidth:e.clientWidth,scrollWidth:e.scrollWidth,clientHeight:e.clientHeight,scrollHeight:e.scrollHeight}))''')
                self.assertEqual(overflow, [])
                self.page.screenshot(path=str(output / f'{width}x{height}-{name}.png'))
                if name == 'widgets':
                    for index, kind in [(1, 'morning'), (2, 'night')]:
                        self.page.evaluate('(index)=>selectWidget(index,0,false)', index)
                        self.page.wait_for_timeout(100)
                        button = self.page.locator('[data-action]').bounding_box()
                        card = self.page.locator('#rail').bounding_box()
                        light = self.page.locator('#light').bounding_box()
                        self.assertGreaterEqual(button['height'], 76)
                        self.assertLessEqual(button['y']+button['height'], card['y']+card['height'])
                        self.assertLess(card['y']+card['height'], light['y'])
                        self.page.screenshot(path=str(output / f'{width}x{height}-{kind}.png'))
                    self.page.evaluate('selectWidget(0,0,false)')
        self.assertEqual(self.posts, [])


if __name__ == '__main__':
    unittest.main()

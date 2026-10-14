# Room Clock

Give an old phone a quiet new job: a large, dark room clock with optional Home Assistant controls, weather, and AI usage rings.

- Readable 12-hour clock and date, with landscape and portrait layouts.
- Swipe vertically between clock-only and widgets; widgets show usage and weather summary/hourly forecasts.
- Optional morning/night greetings and routines, plus a room switch.
- Bundled fonts and icons; no accounts or integrations needed for the clock.

## Run it

Requires **Python 3.11+**, Git, and a modern browser.

```sh
git clone https://github.com/umair-qidwai/Room-Clock.git
cd Room-Clock
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8780
```

Open **http://localhost:8780**. For another device on your trusted network, use `--host 0.0.0.0` and open `http://<server-address>:8780` instead.
Run only one Uvicorn instance with one worker; routine locking and state-file updates are process-local.

## Optional configuration

Copy the blank template outside the repository, then edit it with your own settings:

```sh
mkdir -p "$HOME/.config/room-clock"
cp .env.example "$HOME/.config/room-clock/env"
chmod 600 "$HOME/.config/room-clock/env"
```

See [.env.example](.env.example) for optional Home Assistant entities, a separate compatible AI usage monitor, greetings, and timezone settings. Blank integrations stay disabled. The app reads process environment variables, **not `.env` files automatically**. Source only your own trusted file before starting:

```sh
set -a
. "$HOME/.config/room-clock/env"
set +a
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8780
```

The configured timezone defaults to `America/New_York`; morning state follows the calendar day, while night state uses a noon-to-noon cycle. At Shuruq, tapping the morning greeting requests the routine, shows weather for up to two minutes (tap to dismiss), then opens widgets. A pending morning greeting requests the routine and dismisses at noon. At Isha iqama + 20 minutes, tapping the night greeting requests the routine, then returns to clock-only after acceptance. These are browser-driven prompts, not a background scheduler or a guarantee that Home Assistant finished a routine.

Keep the browser active. Screen wake lock requires a supported browser and secure localhost/HTTPS; the OS can still suspend it. **Controls are unauthenticated: use a trusted network only, not public Internet exposure.**
Forwarded AI usage JSON must not include credentials, tokens, or other secrets.

For Linux boot startup, see the supplied [systemd user service](systemd/room-clock.service). It assumes `~/Room-Clock`, loads the private env file above, and listens on localhost; adjust paths if needed.

## Tests

Run from the repository root; Python and Node checks do not require Chrome:

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -p test_app.py -v
node tests/test_ui.js
node tests/test_clock_recovery.js
node tests/test_frontend.js
```

Optional browser tests require Playwright (included in dev requirements) and installed Chromium. Set `CHROMIUM_PATH` if it is not `/usr/bin/chromium`:

```sh
.venv/bin/python -m unittest discover -s tests -p test_browser.py -v
```

## License

Application: [MIT](LICENSE). The fallback embedded clock font derives from Liberation Fonts and retains its [SIL Open Font License and notices](clock-font-LICENSE.txt). Bundled Fredoka retains its [SIL Open Font License](fredoka-font-LICENSE.txt).

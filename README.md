# Room Clock

**Give an old phone a quiet new job.**

A big, readable room clock with a dark display, swipeable widgets, and optional Home Assistant controls and AI usage rings. Run the tiny Python server on a Raspberry Pi or another always-on computer; open it in your phone's browser.

- Large rounded 12-hour clock and local date
- Landscape-first layout, portrait support, and reduced-motion styling
- Swipe the clock vertically between clock-only and clock-with-widgets pages
- Swipe the widget card between AI usage, morning, and night; AI usage is the default
- Optional weather, prayer-time scheduling, room switch, and routine buttons
- Bundled digits and icons: no CDN or third-party analytics

## Fresh install

Requires **Python 3.11+**, Git, and a modern browser. On Debian/Raspberry Pi OS, install prerequisites with `sudo apt install git python3-venv`.

```sh
git clone https://github.com/umair-qidwai/Room-Clock.git
cd Room-Clock
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8780
```

Open **http://localhost:8780** on that computer. No accounts or integrations are needed for the clock. Unconfigured controls stay disabled; usage values display dashes. This is a server-backed display, not an offline app.

For a temporary test from a phone on your **trusted home network**, replace `127.0.0.1` with `0.0.0.0`, then open `http://<server-address>:8780`. Configure HTTPS before relying on home-screen installation. Do not port-forward this server to the Internet.

## Turn an old phone into a display

1. Connect the phone and server to the same trusted Wi-Fi. Set automatic date/time and the same timezone on both; the clock uses the phone's timezone and daily routine flags use the server's.
2. Open the server address in a recent Chrome/Chromium or Safari. Landscape is recommended. Swipe vertically on the time to switch clock pages, or on the widget card to switch widgets.
3. For an app-like home-screen window, serve **trusted HTTPS**. Use a trusted reverse proxy, or generate a local certificate with a local CA tool such as `mkcert`, including your server's actual LAN hostname/IP. Install **only the CA certificate**, never its private key, as trusted on the phone. Merely clicking through a certificate warning is not equivalent to a trusted secure context.
4. In Chrome choose **Install app / Add to Home screen**; in Safari choose **Share → Add to Home Screen**. Availability and standalone behavior depend on browser/OS. There is no service worker or offline cache. If the browser only offers a shortcut, keep using it in-browser or use a trusted kiosk browser.
5. Adjust display sleep/auto-lock in the OS or kiosk browser if needed. Room Clock requests a browser screen wake lock while visible, but the browser or OS may deny it and the app cannot control system brightness. Use modest brightness, keep ventilation clear, and monitor battery health; do not leave a swollen or overheating old battery charging.

HTTPS directly with Uvicorn (replace certificate filenames with your own):

```sh
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8780 \
  --ssl-keyfile "$HOME/.config/room-clock/tls/server.key" \
  --ssl-certfile "$HOME/.config/room-clock/tls/server.crt"
```

Keep keys outside the repository, mode `600`. No certificate or private CA is shipped here.

## Optional integrations

Copy the blank template outside the source tree:

```sh
mkdir -p "$HOME/.config/room-clock"
cp .env.example "$HOME/.config/room-clock/env"
chmod 600 "$HOME/.config/room-clock/env"
```

Edit that private file locally. All entries are optional. The application reads **process environment variables**, not `.env` files automatically. For an interactive launch, source only your own trusted configuration:

```sh
set -a
. "$HOME/.config/room-clock/env"
set +a
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8780
```

| Variable | Purpose |
| --- | --- |
| `HASS_URL` | Home Assistant base URL, without `/api` |
| `HASS_TOKEN` | Home Assistant long-lived access token; server-side only |
| `ROOM_SWITCH_ENTITY` | Your `switch.*` entity for the corner light button |
| `WEATHER_ENTITY` | Your `weather.*` entity with daily forecast support |
| `FAJR_ENTITY`, `ISHA_ENTITY` | Sensors whose states are parseable date/time strings |
| `MORNING_AUTOMATION_ENTITY`, `NIGHT_AUTOMATION_ENTITY` | Your `automation.*` entities; buttons trigger with `skip_condition=false` |
| `AI_USAGE_URL` | Base URL of a separate compatible usage monitor |

Routine completion is saved in ignored `interaction-state.json` and resets on the server's local date. Morning and night routines never take over the widget rail automatically; AI usage remains the default and the routine buttons are manually accessible by swiping. Setting an automation entity does **not** change its Home Assistant triggers. The room control supports the `switch` domain, not arbitrary `light` entities.

### AI usage data flow

```text
Separate authenticated usage monitor → GET /api/usage
                                      ↓
                              Room Clock backend
                                      ↓ GET /api/state
                              Phone's usage rings
```

AI usage comes **directly from the monitor**, not from Home Assistant. Room Clock does not log in to providers, read browser cookies, or collect usage itself. Run and authenticate your monitor separately; its implementation and private browser profiles are not included.

The monitor must return an object containing optional `codex` and `claude` objects. Each can contain `five_hour_remaining` and `weekly_remaining`, numeric percentages from 0 to 100 (remaining, not used). Missing values render as dashes. Room Clock forwards the monitor's JSON in its local state response, so that endpoint must contain only information you intend to expose to dashboard clients. Provider cookies and tokens must never be returned by your monitor.

### Security boundary

**This app has no user authentication.** Anyone able to reach it can read configured entity/usage data and invoke configured controls. HTTPS encrypts traffic; it does not restrict access. Keep it on a trusted, restricted network. Use an authenticated reverse proxy and appropriate firewall/access rules before any wider access. Do not share live `/api/state` responses, private env files, browser profiles, logs, state files, or TLS keys in issues or commits.

Home Assistant credentials stay on the server and are never embedded in the HTML. Upstream error details are hidden from state responses. Configuration files, caches, backup files, and runtime state are excluded by `.gitignore`; still review every commit for secrets.

## Start at boot with systemd (Linux)

The supplied **user** unit assumes the clone is at `~/Room-Clock`. It listens only on localhost by default, suitable for a local browser or reverse proxy.

```sh
mkdir -p "$HOME/.config/systemd/user"
cp systemd/room-clock.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now room-clock.service
systemctl --user is-active room-clock.service
```

For direct LAN HTTPS, edit the installed unit's `ExecStart` to use `--host 0.0.0.0` and the TLS flags above (use `%h` instead of `$HOME` inside a unit), then reload and restart it. If the clone is elsewhere, update both `WorkingDirectory` and `ExecStart`. To run without an interactive login, an administrator can enable lingering with `sudo loginctl enable-linger "$USER"`.

## Tests

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
```

Tests use isolated configuration and temporary state; they do not access your Home Assistant or trigger real actions.

## License

Application code: [MIT](LICENSE). The fallback embedded clock font derives from
Liberation Fonts and retains its separate [SIL Open Font License and
notices](clock-font-LICENSE.txt). The bundled Fredoka display font retains its
[SIL Open Font License](fredoka-font-LICENSE.txt).

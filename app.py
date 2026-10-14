import json
import logging
import math
import os
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as wall_time, timedelta
from pathlib import Path
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "interaction-state.json"
STATIC = ROOT / "static"
HASS_URL = os.environ.get("HASS_URL", "").rstrip("/")
HASS_TOKEN = os.environ.get("HASS_TOKEN", "")
AI_URL = os.environ.get("AI_USAGE_URL", "").rstrip("/")
USER_NAME = os.environ.get("USER_NAME", "").strip()
ENTITIES = {
    "shuruq": os.environ.get("HA_SHURUQ_ENTITY", ""),
    "isha_iqama": os.environ.get("HA_ISHA_IQAMA_ENTITY", ""),
    "weather": os.environ.get("WEATHER_ENTITY", ""),
    "room_switch": os.environ.get("ROOM_SWITCH_ENTITY", os.environ.get("BOYS_SWITCH_ENTITY", "")),
}
MORNING_AUTOMATION = os.environ.get("MORNING_AUTOMATION_ENTITY", "")
NIGHT_AUTOMATION = os.environ.get("NIGHT_AUTOMATION_ENTITY", "")
TIMEZONE = ZoneInfo(os.environ.get("ROOM_CLOCK_TIMEZONE", "America/New_York"))
STATE_LOCK = threading.Lock()
log = logging.getLogger("room-clock")
app = FastAPI(title="Room Clock")

def today():
    return datetime.now(TIMEZONE).date().isoformat()


def night_cycle_date():
    # A bedtime routine after midnight belongs to the preceding evening.
    # Reset at local noon, independently of the morning's calendar-day reset.
    return (datetime.now(TIMEZONE) - timedelta(hours=12)).date().isoformat()
def fresh_state():
    return {
        "date": today(),
        "night_date": night_cycle_date(),
        "morning_done": False,
        "night_done": False,
        "morning_weather_until": 0,
        "morning_inflight": False,
        "night_inflight": False,
    }


def load_state():
    # Callers hold STATE_LOCK across the entire load/reset/save transaction.
    try:
        state_data = json.loads(STATE_FILE.read_text())
        if not isinstance(state_data, dict):
            raise ValueError("State must be an object")
        for field in ("date", "night_date"):
            if field == "date" or field in state_data:
                value = state_data[field]
                if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                    raise ValueError("Invalid cycle date")
        for field in ("morning_done", "night_done", "morning_inflight", "night_inflight"):
            value = state_data[field] if field.endswith("_done") else state_data.get(field, False)
            if not isinstance(value, bool):
                raise ValueError("Invalid completion marker")
        state_data.setdefault("morning_weather_until", 0)
        deadline = state_data["morning_weather_until"]
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("Invalid weather deadline")
        return state_data
    except FileNotFoundError:
        return fresh_state()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.exception("Interaction state could not be read; preserving the existing file")
        raise HTTPException(503, "Interaction state unavailable; routines are disabled until it is repaired") from exc


def save_state(s):
    temporary = STATE_FILE.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(s, indent=2) + "\n")
        os.replace(temporary, STATE_FILE)
    except OSError as exc:
        log.exception("Interaction state could not be saved")
        raise HTTPException(503, "Interaction state could not be saved") from exc


def reset_daily(s):
    previous = dict(s)
    night_date = s.get("night_date", s.get("date"))
    # Legacy calendar-day markers from today also belong to this night cycle,
    # including after midnight; older dates still need to match the cycle.
    if "night_date" not in s and s.get("date") == today():
        night_date = night_cycle_date()
    night_done, night_inflight = s.get("night_done", False), s.get("night_inflight", False)
    if s.get("date") != today():
        s = fresh_state()
    if night_date == night_cycle_date():
        s.update(night_date=night_date, night_done=night_done, night_inflight=night_inflight)
    else:
        s.update(night_date=night_cycle_date(), night_done=False, night_inflight=False)
    if s != previous:
        save_state(s)
    return s

class HARejected(RuntimeError):
    """The command was definitely not accepted; an explicit retry is safe."""


def ha(path, method="GET", payload=None):
    if not HASS_URL or not HASS_TOKEN:
        raise HARejected("Home Assistant credentials are not configured")
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        HASS_URL + path, data=data, method=method,
        headers={"Authorization": f"Bearer {HASS_TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except HTTPError as exc:
        # Timeout/server errors can follow execution; retain the safety marker.
        if 400 <= exc.code < 500 and exc.code != 408:
            raise HARejected("Home Assistant rejected the request") from exc
        raise

def state(entity):
    return ha(f"/api/states/{entity}") if entity else {}

def schedule_state(entity, day):
    """Date time-only sensors in the backend calendar; retain timestamp sensors."""
    if not isinstance(entity, dict):
        return entity
    raw = entity.get("state")
    if not isinstance(raw, str) or not re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?", raw.strip()):
        return entity
    try:
        hour, rest = raw.strip().split(":", 1)
        clock_time = wall_time.fromisoformat(f"{hour.zfill(2)}:{rest}")
        occurrence = datetime.combine(date.fromisoformat(day), clock_time, TIMEZONE)
    except ValueError:
        return entity
    return {**entity, "state": occurrence.isoformat()}

def ai_usage():
    if not AI_URL: return {}
    with urllib.request.urlopen(AI_URL + "/api/usage", timeout=5) as r: return json.loads(r.read())
def weather_forecast(forecast_type="daily"):
    if not ENTITIES["weather"]: return []
    try: response = ha("/api/services/weather/get_forecasts?return_response", "POST", {"entity_id": ENTITIES["weather"], "type": forecast_type})
    except Exception: return []
    def find(v):
        if isinstance(v, dict):
            if isinstance(v.get("forecast"), list): return v["forecast"]
            for x in v.values():
                found = find(x)
                if found is not None: return found
        return None
    return find(response) or []

@app.get("/api/state")
def dashboard_state():
    with STATE_LOCK:
        s = reset_daily(load_state())
    result = {
        "date": s["date"],
        "night_date": s["night_date"],
        "timezone": TIMEZONE.key,
        "morning_done": s["morning_done"],
        "night_done": s["night_done"],
        "morning_weather_until": s.get("morning_weather_until", 0),
        "user_name": USER_NAME,
        "entities": ENTITIES,
        "morning_automation_configured": bool(MORNING_AUTOMATION),
        "night_automation_configured": bool(NIGHT_AUTOMATION),
    }
    # These are independent network calls. Fetch them concurrently so a slow weather
    # forecast cannot hold up the clock, light state, or AI rings.
    jobs = {
        "shuruq": lambda: state(ENTITIES["shuruq"]),
        "isha_iqama": lambda: state(ENTITIES["isha_iqama"]),
        "weather": lambda: state(ENTITIES["weather"]),
        "room": lambda: state(ENTITIES["room_switch"]),
        "usage": ai_usage,
    }
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {pool.submit(fn): key for key, fn in jobs.items()}
        for future, key in futures.items():
            try: result[key] = future.result()
            except Exception:
                result.setdefault("errors", {})[key] = "Integration unavailable"
    for key in ("shuruq", "isha_iqama"):
        if key in result:
            result[key] = schedule_state(result[key], s["date"])
    # The browser anchors its clock at receipt, after integrations have completed.
    result["server_now"] = time.time()
    return result

@app.get("/api/weather")
def weather_data():
    with ThreadPoolExecutor(max_workers=2) as pool:
        daily = pool.submit(weather_forecast, "daily")
        hourly = pool.submit(weather_forecast, "hourly")
        return {"forecast": daily.result(), "hourly": hourly.result()}


@app.post("/api/room/toggle")
def toggle_room():
    if not ENTITIES["room_switch"]: raise HTTPException(409, "Room switch is not configured")
    current = state(ENTITIES["room_switch"])
    service = "turn_off" if current.get("state") == "on" else "turn_on"
    expected = "off" if service == "turn_off" else "on"
    # Return as soon as HA accepts the command. The browser confirms the final
    # state separately so the button feels immediate without claiming success.
    ha(f"/api/services/switch/{service}", "POST", {"entity_id": ENTITIES["room_switch"]})
    return {"accepted": True, "requested_state": expected, "state": current}

def run_automation(entity, key):
    if not entity: raise HTTPException(409, f"{key} automation placeholder is not configured yet")
    # Serialize the check, HA call, and persistence so double taps and concurrent
    # requests cannot trigger the same routine twice in one daily cycle.
    with STATE_LOCK:
        s = reset_daily(load_state())
        if s.get(key + "_done"):
            return {"ok": True, "triggered": entity, "already_done": True, "morning_weather_until": s.get("morning_weather_until", 0)}
        # A persisted marker may precede or follow HA acceptance. Never guess
        # completion or resend a side effect whose outcome is unknown.
        if s.get(key + "_inflight"):
            raise HTTPException(503, f"The {key} routine outcome is unknown; not retried")
        s[key + "_inflight"] = True
        save_state(s)
        try:
            ha("/api/services/automation/trigger", "POST", {"entity_id": entity, "skip_condition": False})
        except HARejected as exc:
            s[key + "_inflight"] = False
            save_state(s)
            log.exception("Home Assistant %s routine was not accepted", key)
            raise HTTPException(503, f"Could not start the {key} routine") from exc
        except Exception as exc:
            log.exception("Home Assistant %s routine outcome is unknown", key)
            raise HTTPException(503, f"The {key} routine outcome is unknown; not retried") from exc
        s[key + "_done"] = True
        s[key + "_inflight"] = False
        if key == "morning":
            s["morning_weather_until"] = time.time() + 120
        try:
            save_state(s)
        except HTTPException as exc:
            raise HTTPException(503, f"The {key} routine outcome is unknown; completion could not be saved") from exc
    return {"ok": True, "triggered": entity, "already_done": False, "morning_weather_until": s.get("morning_weather_until", 0)}

@app.post("/api/morning")
def morning(): return run_automation(MORNING_AUTOMATION, "morning")
@app.post("/api/night")
def night(): return run_automation(NIGHT_AUTOMATION, "night")
@app.get("/manifest.webmanifest")
def manifest(): return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

@app.get("/icon.svg")
def icon(): return FileResponse(STATIC / "icon.svg", media_type="image/svg+xml")

@app.get("/icon-192.png")
def icon_192(): return FileResponse(STATIC / "icon-192.png", media_type="image/png")

@app.get("/icon-512.png")
def icon_512(): return FileResponse(STATIC / "icon-512.png", media_type="image/png")

@app.get("/oswald-clock.ttf")
def reference_clock_font(): return FileResponse(STATIC / "oswald-clock.ttf", media_type="font/ttf")

@app.get("/oxanium-clear-zero.ttf")
def graphite_clock_font(): return FileResponse(STATIC / "oxanium-clear-zero.ttf", media_type="font/ttf")

@app.get("/fredoka-bold.ttf")
def clock_font(): return FileResponse(STATIC / "fredoka-bold.ttf", media_type="font/ttf")

@app.get("/")
def index(): return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

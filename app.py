import json, os, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "interaction-state.json"
STATIC = ROOT / "static"
HASS_URL = os.environ.get("HASS_URL", "").rstrip("/")
HASS_TOKEN = os.environ.get("HASS_TOKEN", "")
AI_URL = os.environ.get("AI_USAGE_URL", "").rstrip("/")
ENTITIES = {"fajr": os.environ.get("FAJR_ENTITY", ""), "isha": os.environ.get("ISHA_ENTITY", ""), "weather": os.environ.get("WEATHER_ENTITY", ""), "room_switch": os.environ.get("ROOM_SWITCH_ENTITY", os.environ.get("BOYS_SWITCH_ENTITY", ""))}
MORNING_AUTOMATION = os.environ.get("MORNING_AUTOMATION_ENTITY", "")
NIGHT_AUTOMATION = os.environ.get("NIGHT_AUTOMATION_ENTITY", "")
app = FastAPI(title="Room Clock")

def today(): return datetime.now().astimezone().date().isoformat()
def load_state():
    try: return json.loads(STATE_FILE.read_text())
    except Exception: return {"date": today(), "morning_done": False, "night_done": False}
def save_state(s): STATE_FILE.write_text(json.dumps(s, indent=2) + "\n")
def reset_daily(s):
    if s.get("date") != today(): s = {"date": today(), "morning_done": False, "night_done": False}; save_state(s)
    return s

def ha(path, method="GET", payload=None):
    if not HASS_URL or not HASS_TOKEN: raise RuntimeError("Home Assistant credentials are not configured")
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(HASS_URL + path, data=data, method=method, headers={"Authorization": f"Bearer {HASS_TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r: return json.loads(r.read())

def state(entity): return ha(f"/api/states/{entity}") if entity else {}
def ai_usage():
    if not AI_URL: return {}
    with urllib.request.urlopen(AI_URL + "/api/usage", timeout=5) as r: return json.loads(r.read())
def weather_forecast():
    if not ENTITIES["weather"]: return []
    try: response = ha("/api/services/weather/get_forecasts?return_response", "POST", {"entity_id": ENTITIES["weather"], "type": "daily"})
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
    s = reset_daily(load_state())
    result = {"date": s["date"], "morning_done": s["morning_done"], "night_done": s["night_done"], "entities": ENTITIES, "morning_automation_configured": bool(MORNING_AUTOMATION), "night_automation_configured": bool(NIGHT_AUTOMATION)}
    # These are independent network calls. Fetch them concurrently so a slow weather
    # forecast cannot hold up the clock, light state, or AI rings.
    jobs = {
        "fajr": lambda: state(ENTITIES["fajr"]),
        "isha": lambda: state(ENTITIES["isha"]),
        "weather": lambda: state(ENTITIES["weather"]),
        "room": lambda: state(ENTITIES["room_switch"]),
        "usage": ai_usage,
    }
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {pool.submit(fn): key for key, fn in jobs.items()}
        for future, key in ((future, futures[future]) for future in futures):
            try: result[key] = future.result()
            except Exception:
                result.setdefault("errors", {})[key] = "Integration unavailable"
    return result

@app.get("/api/weather")
def weather_data(): return {"forecast": weather_forecast()}

def wait_for_state(entity, expected, timeout=6):
    # Return HA's confirmed state, not the state we requested.
    deadline = time.monotonic() + timeout
    latest = {"entity_id": entity, "state": "unknown"}
    while time.monotonic() < deadline:
        try:
            latest = state(entity)
            if latest.get("state") == expected:
                return latest
        except Exception:
            pass
        time.sleep(0.25)
    try:
        return state(entity)
    except Exception:
        return latest

@app.post("/api/room/toggle")
def toggle_room():
    if not ENTITIES["room_switch"]: raise HTTPException(409, "Room switch is not configured")
    current = state(ENTITIES["room_switch"])
    service = "turn_off" if current.get("state") == "on" else "turn_on"
    expected = "off" if service == "turn_off" else "on"
    # Return as soon as HA accepts the command. The browser confirms the final
    # state separately so the button feels immediate without claiming success.
    ha(f"/api/services/switch/{service}", "POST", {"entity_id": ENTITIES["room_switch"]})
    return {"accepted": True, "requested_state": expected, "state": state(ENTITIES["room_switch"])}

def run_automation(entity, key):
    if not entity: raise HTTPException(409, f"{key} automation placeholder is not configured yet")
    ha("/api/services/automation/trigger", "POST", {"entity_id": entity, "skip_condition": False})
    s = reset_daily(load_state()); s[key + "_done"] = True; save_state(s)
    return {"ok": True, "triggered": entity}

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

@app.get("/fredoka-bold.ttf")
def clock_font(): return FileResponse(STATIC / "fredoka-bold.ttf", media_type="font/ttf")

@app.get("/")
def index(): return FileResponse(STATIC / "index.html")

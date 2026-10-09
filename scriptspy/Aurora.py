# Aurora.py: realtime voice client for the Aion desktop avatar, on the OpenAI Live API (gpt-live-1)
#
# Input : microphone -> wss://api.openai.com/v1/live/sessions (full duplex)
# Output: assistant audio -> VB-CABLE ("CABLE Input") for VMagicMirror lip sync,
#         plus a monitor device (headphones or speakers) so you can hear her.
#
# "Aurora" is the script's original name; the assistant persona is called Aion.
#
# Design notes:
#   - Live API protocol: session.start, session.input_audio.append,
#     session.output_audio.delta, session.input_transcript.*, session.close.
#   - The opening greeting is sent with session.commentary.append.
#   - Playback is callback driven with a shared buffer, so the asyncio loop never blocks.
#   - Device selection skips the WDM-KS host API (PortAudio cannot open it in this
#     stream mode), prefers MME (it resamples 24 kHz for us), then DirectSound, then
#     WASAPI, and falls back to the Windows default output if no monitor matches.
#   - End phrase: "Thank you Aion, that is all for now".
#
# Configuration: copy .env.example to .env in the project root and fill it in.
# Every value can also be set as a normal environment variable, which wins over .env.
#
#   OPENAI_API_KEY          API key (or keys/openai.key in the project root)
#   AION_USER_NAME          how Aion addresses you (example "Dr. Smith"); empty = she asks
#   AURORA_LIVE_MODEL       default gpt-live-1
#   AURORA_VOICE            default gleam (gpt-live-1 voices: quartz, ripple, vesper, willow, stone, gleam, meridian, bossa, tempo, beacon, delta, cinder; marin also works)
#   AURORA_MODE             default | lecture | grading | job_search | tech_support
#   AURORA_BOOT_PROMPT      override the greeting direction ("" to launch silently); default uses the local time of day
#   AURORA_BACKEND_MODEL    set to a Responses model id to enable delegation (off by default)
#   AURORA_IN_DEV           mic device index (from the printed device list)
#   AURORA_OUT_VB           VB-CABLE sink index
#   AURORA_OUT_MON          monitor (headphones/speakers) index
#   AURORA_MIC_KEYWORDS     comma list matched against device names to pick the mic (default "microphone")
#   AURORA_MONITOR_KEYWORDS comma list matched against device names to pick the monitor (default "headphones,speakers")
#   AURORA_NO_MONITOR=1     lip-sync sink only, no monitor
#   AURORA_STARTUP_BEEP=1   short beep on the outputs at startup
#   AURORA_PROFILE_PATH     your personal profile, .md or .json (default config/profile.md)
#   AURORA_MEMORY_PATH      long-term memory notes (default memory/aion_memory.md)
#   AURORA_NO_CALENDAR=1    skip the Google Calendar lookup
#   AURORA_EXTRACT_MODEL    text model used to parse "add to my calendar" requests (default gpt-5-mini)
#
# Google Calendar (read, add, edit, undo) needs keys/google_credentials.json (OAuth desktop client from Google Cloud).
# The first run opens a browser for consent and saves keys/google_token.json; later runs are silent.

import asyncio
import base64
import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Optional, List, Dict

import numpy as np
import sounddevice as sd
import websockets

# ---------- Logging when there is no console (built with --windowed, launched from a .bat) ----------
def _attach_file_logs():
    if sys.stdout is not None and sys.stderr is not None and not getattr(sys, "frozen", False):
        return
    base = Path(sys.executable).parent.parent if getattr(sys, "frozen", False) else Path(__file__).parent.parent
    logs = base / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
        for p in (logs / "aurora_stdout.log", logs / "aurora_stderr.log"):
            if p.exists() and p.stat().st_size > 5_000_000:     # keep the logs from growing forever
                p.unlink()
        sys.stdout = open(logs / "aurora_stdout.log", "a", encoding="utf-8", buffering=1)
        sys.stderr = open(logs / "aurora_stderr.log", "a", encoding="utf-8", buffering=1)
        print(f"\n===== Aion start {__import__('time').strftime('%Y-%m-%d %H:%M:%S')} =====", flush=True)
    except Exception:
        pass

_attach_file_logs()

# Project root: <root>\dist\Aurora.exe or <root>\scriptspy\Aurora.py
ROOT = Path(sys.executable).parent.parent if getattr(sys, "frozen", False) else Path(__file__).parent.parent

# ---------- .env loader (no extra dependency; real environment variables take precedence) ----------
def _load_dotenv(path: Path):
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception as e:
        print(f"[config] could not read {path}: {e}", flush=True)

_load_dotenv(ROOT / ".env")

# ---------- Config ----------
USER_NAME = os.getenv("AION_USER_NAME", "").strip()        # empty = Aion asks what to call you
MODEL = os.getenv("AURORA_LIVE_MODEL", "gpt-live-1")
VOICE = os.getenv("AURORA_VOICE", "gleam")
BACKEND_MODEL = os.getenv("AURORA_BACKEND_MODEL", "").strip()   # empty = no delegation
SAMPLE_RATE = 24000
CHUNK_MS = 40
CHUNK_SAMPLES = int(SAMPLE_RATE * CHUNK_MS / 1000)
MIC_QUEUE_MAX = 100          # ~4 s of audio; oldest chunks are dropped if the link stalls
PLAYBACK_BLOCK = 480         # 20 ms callback blocks

WS_URL = "wss://api.openai.com/v1/live/sessions"
USER_AGENT = "aion-avatar/2.0 (python)"

def _time_of_day(hour: int) -> str:
    if hour < 12:
        return "morning"
    if hour < 17:
        return "afternoon"
    return "evening"

def _who() -> str:
    """How prompts refer to the user: the configured name, or 'The user'."""
    return USER_NAME or "The user"

def boot_prompt() -> str:
    """Direction for the opening greeting. She composes the words herself, so it varies each launch.
    Set AURORA_BOOT_PROMPT to override, or to "" to launch silently."""
    custom = os.getenv("AURORA_BOOT_PROMPT")
    if custom is not None:
        return custom.strip()
    import datetime
    now = datetime.datetime.now()
    return (
        f"{_who()} just launched you. It is {now.strftime('%A')} {_time_of_day(now.hour)}, "
        f"about {now.strftime('%I:%M %p').lstrip('0')} local time. "
        "Greet the user warmly in one or two natural sentences, the way a colleague would when someone walks into the room. "
        "Use the time of day in the greeting. Vary the wording every time. "
        + ("If something is on the user's calendar today, mention the next item briefly. " if CALENDAR_TEXT else "")
        + "Do not announce that you are online, do not describe yourself, and do not list options."
    )

PROFILE_PATH = Path(os.getenv("AURORA_PROFILE_PATH", str(ROOT / "config" / "profile.md")))
MEMORY_PATH = Path(os.getenv("AURORA_MEMORY_PATH", str(ROOT / "memory" / "aion_memory.md")))
GOOGLE_CREDENTIALS = ROOT / "keys" / "google_credentials.json"
GOOGLE_TOKEN = ROOT / "keys" / "google_token.json"
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]   # read + add/remove events
EXTRACT_MODEL = os.getenv("AURORA_EXTRACT_MODEL", "gpt-5-mini")             # small text model that turns a spoken request into an event

# ---------- Long-term memory (a plain text file she reads at launch and can add to by voice) ----------
def load_memory() -> str:
    try:
        if MEMORY_PATH.exists():
            text = MEMORY_PATH.read_text(encoding="utf-8").strip()
            if text:
                return text
    except Exception as e:
        print(f"[memory] could not read {MEMORY_PATH}: {e}", flush=True)
    return ""

def remember(note: str) -> bool:
    """Append one dated line to the memory file."""
    import datetime
    note = note.strip().rstrip(".").strip()
    if not note:
        return False
    try:
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        new_file = not MEMORY_PATH.exists()
        with open(MEMORY_PATH, "a", encoding="utf-8") as f:
            if new_file:
                f.write("# Aion memory\n\n## Notes added by voice\n")
            f.write(f"- [{datetime.date.today().isoformat()}] {note}\n")
        print(f"[memory] saved: {note}", flush=True)
        return True
    except Exception as e:
        print(f"[memory] could not write {MEMORY_PATH}: {e}", flush=True)
        return False

# ---------- Google Calendar (read-only) ----------
def _google_credentials():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    creds = None
    if GOOGLE_TOKEN.exists():
        try:
            stored_scopes = set(json.loads(GOOGLE_TOKEN.read_text(encoding="utf-8")).get("scopes") or [])
        except Exception:
            stored_scopes = set()
        creds = Credentials.from_authorized_user_file(str(GOOGLE_TOKEN), CALENDAR_SCOPES)
        if not set(CALENDAR_SCOPES) <= stored_scopes:
            print("[calendar] stored token lacks the events scope; asking for consent again", flush=True)
            creds = None
            try:
                GOOGLE_TOKEN.unlink()
            except Exception:
                pass
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not GOOGLE_CREDENTIALS.exists():
            print(f"[calendar] no {GOOGLE_CREDENTIALS}; calendar disabled", flush=True)
            return None
        from google_auth_oauthlib.flow import InstalledAppFlow
        print("[calendar] first run: opening the browser for Google consent", flush=True)
        flow = InstalledAppFlow.from_client_secrets_file(str(GOOGLE_CREDENTIALS), CALENDAR_SCOPES)
        creds = flow.run_local_server(port=0)
    GOOGLE_TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return creds

def calendar_summary(hours_ahead: int = 36) -> str:
    """Upcoming events from now until the end of tomorrow, as plain lines. Empty string on any failure."""
    if os.getenv("AURORA_NO_CALENDAR") == "1":
        return ""
    try:
        import datetime, requests
        creds = _google_credentials()
        if creds is None:
            return ""
        now = datetime.datetime.now().astimezone()
        end = (now + datetime.timedelta(hours=hours_ahead)).replace(hour=23, minute=59, second=59)
        r = requests.get(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers={"Authorization": f"Bearer {creds.token}"},
            params={"timeMin": now.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                    "orderBy": "startTime", "maxResults": 25},
            timeout=10,
        )
        r.raise_for_status()
        def day_label(d: datetime.date) -> str:
            if d == now.date():
                return "Today"
            if d == now.date() + datetime.timedelta(days=1):
                return f"Tomorrow ({d.strftime('%A')})"
            return d.strftime("%A %B %d").replace(" 0", " ")
        lines = []
        for ev in r.json().get("items", []):
            if ev.get("status") == "cancelled":
                continue
            title = ev.get("summary", "(untitled)")
            start = ev.get("start", {})
            if "dateTime" in start:
                st = datetime.datetime.fromisoformat(start["dateTime"]).astimezone()
                et = None
                if "dateTime" in ev.get("end", {}):
                    et = datetime.datetime.fromisoformat(ev["end"]["dateTime"]).astimezone()
                span = st.strftime('%I:%M %p').lstrip('0') + (f" to {et.strftime('%I:%M %p').lstrip('0')}" if et else "")
                note = " (in progress now)" if st <= now and (et is None or et > now) else ""
                lines.append(f"- {day_label(st.date())} {span}{note}: {title}")
            else:
                d = datetime.date.fromisoformat(start.get("date", now.date().isoformat()))
                lines.append(f"- {day_label(d)} (all day): {title}")
        text = "\n".join(lines) if lines else "- Nothing scheduled through tomorrow."
        print(f"[calendar] local time {now.strftime('%I:%M %p').lstrip('0')} {now.tzname()} (UTC{now.strftime('%z')}); {len(lines)} upcoming event(s):\n{text}", flush=True)
        return text
    except Exception as e:
        print(f"[calendar] lookup failed: {e}", flush=True)
        return ""

CALENDAR_TEXT = ""      # filled once at startup by main()
LAST_EVENT_ID = ""      # for "undo that"

def extract_event(utterance: str) -> Optional[dict]:
    """Ask a small text model to turn the spoken request into a calendar event. Returns None if it cannot."""
    import datetime, requests
    now = datetime.datetime.now().astimezone()
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "ok": {"type": "boolean", "description": "true only if a title and a start time could be determined"},
            "title": {"type": "string"},
            "start": {"type": "string", "description": "RFC3339 local time with offset, or YYYY-MM-DD for an all-day event"},
            "end": {"type": "string", "description": "RFC3339 local time with offset; for all-day use the day after start. Default one hour after start"},
            "all_day": {"type": "boolean"},
            "problem": {"type": "string", "description": "what was missing or ambiguous, empty if ok"},
        },
        "required": ["ok", "title", "start", "end", "all_day", "problem"],
    }
    prompt = (
        f"Current local time: {now.isoformat()} ({now.strftime('%A')}). "
        "Turn the request below into one calendar event. Resolve relative words (tomorrow, Sunday, next week) against the current time. "
        "'Tomorrow' means the next calendar day. If no day is given, use today when the time is still ahead, otherwise tomorrow. "
        "If AM or PM is not stated, assume the working-day reading (1 to 6 means PM, 7 to 11 means AM). 'Between one and four' means a 1:00 to 4:00 block. "
        "If a duration is given, use it. Keep the title short and in Title Case, and leave out times and dates. "
        "Keep proper names exactly as spoken even when they contain a day word: 'Sunday Market' is a business name and stays 'Sunday Market'. "
        "If something essential is still missing, set ok=false and put a single short question in 'problem'. "
        f"Request: \"{utterance}\""
    )
    try:
        r = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": EXTRACT_MODEL, "input": prompt,
                  "text": {"format": {"type": "json_schema", "name": "calendar_event", "strict": True, "schema": schema}}},
            timeout=20,
        )
        r.raise_for_status()
        out = r.json()
        text = ""
        for item in out.get("output", []):
            for c in item.get("content", []) or []:
                if c.get("type") in ("output_text", "text"):
                    text += c.get("text", "")
        data = json.loads(text)
        print(f"[calendar] extracted: {data}", flush=True)
        return data
    except Exception as e:
        print(f"[calendar] extraction failed: {e}", flush=True)
        return None

def matching_color(creds, title: str) -> Optional[str]:
    """Color of the most recent past event with the same title, so new entries match the user's existing labels."""
    import datetime, requests
    try:
        since = (datetime.datetime.now().astimezone() - datetime.timedelta(days=120)).isoformat()
        r = requests.get(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers={"Authorization": f"Bearer {creds.token}"},
            params={"q": title, "timeMin": since, "singleEvents": "true", "orderBy": "startTime", "maxResults": 50},
            timeout=10,
        )
        r.raise_for_status()
        for ev in reversed(r.json().get("items", [])):
            if ev.get("summary", "").strip().lower() == title.strip().lower() and ev.get("colorId"):
                return ev["colorId"]
    except Exception as e:
        print(f"[calendar] color lookup failed: {e}", flush=True)
    return None

def add_calendar_event(utterance: str):
    """Create the event. Returns (sentence for Aion to say, needs_more_details)."""
    global LAST_EVENT_ID, LAST_EDIT, CALENDAR_TEXT
    import datetime, requests
    data = extract_event(utterance)
    if not data:
        return "I could not work out that event. Please say the day, the time, and what it is.", True
    if not data.get("ok"):
        return f"I need one more detail. {data.get('problem') or 'Which day and what time?'}", True
    try:
        creds = _google_credentials()
        if creds is None:
            return "The calendar is not connected on this machine.", False
        if data.get("all_day"):
            body = {"summary": data["title"], "start": {"date": data["start"][:10]}, "end": {"date": data["end"][:10]}}
        else:
            body = {"summary": data["title"], "start": {"dateTime": data["start"]}, "end": {"dateTime": data["end"]}}
        color = matching_color(creds, data["title"])
        if color:
            body["colorId"] = color
            print(f"[calendar] reusing color {color} from an earlier '{data['title']}'", flush=True)
        r = requests.post(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers={"Authorization": f"Bearer {creds.token}"}, json=body, timeout=10,
        )
        r.raise_for_status()
        ev = r.json()
        LAST_EVENT_ID = ev.get("id", "")
        LAST_EDIT = None
        st = data["start"]
        if data.get("all_day"):
            when = datetime.date.fromisoformat(st[:10]).strftime("%A %B %d").replace(" 0", " ") + ", all day"
        else:
            sd = datetime.datetime.fromisoformat(st).astimezone()
            ed = datetime.datetime.fromisoformat(data["end"]).astimezone()
            when = f"{sd.strftime('%A')} from {sd.strftime('%I:%M %p').lstrip('0')} to {ed.strftime('%I:%M %p').lstrip('0')}"
        print(f"[calendar] added {ev.get('id')}: {data['title']} ({when})", flush=True)
        CALENDAR_TEXT = calendar_summary()
        return f"Added {data['title']}, {when}. Say 'undo that' if it is wrong.", False
    except Exception as e:
        print(f"[calendar] add failed: {e}", flush=True)
        return "The calendar did not accept that event. Please check the logs.", False

LAST_EDIT = None        # (event_id, previous fields) so an edit can be undone

def _fmt_when(ev: dict) -> str:
    import datetime
    st = ev.get("start", {})
    if "dateTime" in st:
        sd = datetime.datetime.fromisoformat(st["dateTime"]).astimezone()
        out = f"{sd.strftime('%A')} from {sd.strftime('%I:%M %p').lstrip('0')}"
        if "dateTime" in ev.get("end", {}):
            ed = datetime.datetime.fromisoformat(ev["end"]["dateTime"]).astimezone()
            out += f" to {ed.strftime('%I:%M %p').lstrip('0')}"
        return out
    d = datetime.date.fromisoformat(st.get("date", "1970-01-01"))
    return d.strftime("%A %B %d").replace(" 0", " ") + ", all day"

def extract_edit(utterance: str) -> Optional[dict]:
    """Ask the text model what to change on which event."""
    import datetime, requests
    now = datetime.datetime.now().astimezone()
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "ok": {"type": "boolean"},
            "refers_to_last": {"type": "boolean", "description": "true if the user means the event just added or edited ('that', 'it', 'the one you just made')"},
            "target_title": {"type": "string", "description": "words identifying the event to change, empty if refers_to_last"},
            "target_date": {"type": "string", "description": "YYYY-MM-DD of the event to change if stated, else empty"},
            "new_title": {"type": "string", "description": "empty if unchanged"},
            "new_start": {"type": "string", "description": "RFC3339 with offset, empty if unchanged"},
            "new_end": {"type": "string", "description": "RFC3339 with offset, empty if unchanged"},
            "color_like": {"type": "string", "description": "title of another event whose color/label should be copied, empty if none"},
            "problem": {"type": "string"},
        },
        "required": ["ok", "refers_to_last", "target_title", "target_date", "new_title", "new_start", "new_end", "color_like", "problem"],
    }
    prompt = (
        f"Current local time: {now.isoformat()} ({now.strftime('%A')}). "
        "The user wants to change an existing calendar event. Work out which event and what changes. "
        "Keep proper names exactly as spoken ('Sunday Market' is a business name). A 'label' or 'color' request means color_like. "
        "If the user moves only the start, leave new_end empty. Set ok=false with one short question in 'problem' only if the change itself is unclear. "
        f"Request: \"{utterance}\""
    )
    try:
        r = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": EXTRACT_MODEL, "input": prompt,
                  "text": {"format": {"type": "json_schema", "name": "calendar_edit", "strict": True, "schema": schema}}},
            timeout=20,
        )
        r.raise_for_status()
        text = ""
        for item in r.json().get("output", []):
            for c in item.get("content", []) or []:
                if c.get("type") in ("output_text", "text"):
                    text += c.get("text", "")
        data = json.loads(text)
        print(f"[calendar] edit extracted: {data}", flush=True)
        return data
    except Exception as e:
        print(f"[calendar] edit extraction failed: {e}", flush=True)
        return None

def find_event(creds, title: str, date: str) -> Optional[dict]:
    """Best upcoming (or today's) event whose title contains the words given, optionally on a date."""
    import datetime, requests
    now = datetime.datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + datetime.timedelta(days=21)
    if date:
        try:
            d = datetime.date.fromisoformat(date)
            start = datetime.datetime.combine(d, datetime.time.min).astimezone()
            end = start + datetime.timedelta(days=1)
        except Exception:
            pass
    r = requests.get(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events",
        headers={"Authorization": f"Bearer {creds.token}"},
        params={"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true", "orderBy": "startTime", "maxResults": 100},
        timeout=10,
    )
    r.raise_for_status()
    words = [w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in ("the", "event", "one", "my", "a", "an")]
    best, best_score = None, 0
    for ev in r.json().get("items", []):
        summ = ev.get("summary", "").lower()
        score = sum(1 for w in words if w in summ)
        if score > best_score:
            best, best_score = ev, score
    return best

def edit_calendar_event(utterance: str):
    """Change an existing event. Returns (sentence for Aion to say, needs_more_details)."""
    global LAST_EVENT_ID, LAST_EDIT, CALENDAR_TEXT
    import requests
    data = extract_edit(utterance)
    if not data:
        return "I could not work out that change. Please say which event and what to change.", True
    if not data.get("ok"):
        return f"I need one more detail. {data.get('problem') or 'What should change?'}", True
    try:
        creds = _google_credentials()
        if creds is None:
            return "The calendar is not connected on this machine.", False
        headers = {"Authorization": f"Bearer {creds.token}"}
        ev = None
        if (data.get("refers_to_last") or not data.get("target_title")) and LAST_EVENT_ID:
            r = requests.get(f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{LAST_EVENT_ID}", headers=headers, timeout=10)
            if r.ok:
                ev = r.json()
        if ev is None and data.get("target_title"):
            ev = find_event(creds, data["target_title"], data.get("target_date", ""))
        if ev is None:
            return "I could not find that event on the calendar.", False
        patch = {}
        if data.get("new_title"):
            patch["summary"] = data["new_title"]
        if data.get("new_start"):
            patch["start"] = {"dateTime": data["new_start"]}
            if data.get("new_end"):
                patch["end"] = {"dateTime": data["new_end"]}
            elif "dateTime" in ev.get("end", {}) and "dateTime" in ev.get("start", {}):
                import datetime
                dur = datetime.datetime.fromisoformat(ev["end"]["dateTime"]) - datetime.datetime.fromisoformat(ev["start"]["dateTime"])
                patch["end"] = {"dateTime": (datetime.datetime.fromisoformat(data["new_start"]) + dur).isoformat()}
        elif data.get("new_end"):
            patch["end"] = {"dateTime": data["new_end"]}
        if data.get("color_like"):
            color = matching_color(creds, data["color_like"])
            if color:
                patch["colorId"] = color
            else:
                return f"I could not find an earlier event called {data['color_like']} to copy the color from.", False
        elif "summary" in patch:
            color = matching_color(creds, patch["summary"])
            if color:
                patch["colorId"] = color
        if not patch:
            return "I did not catch what to change.", True
        previous = {k: ev.get(k) for k in patch}
        r = requests.patch(f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{ev['id']}", headers=headers, json=patch, timeout=10)
        r.raise_for_status()
        new_ev = r.json()
        LAST_EVENT_ID = ev["id"]
        LAST_EDIT = (ev["id"], previous)
        print(f"[calendar] edited {ev['id']}: {patch}", flush=True)
        CALENDAR_TEXT = calendar_summary()
        desc = f"{new_ev.get('summary')}, {_fmt_when(new_ev)}"
        if "colorId" in patch:
            desc += ", with the matching color"
        return f"Updated: {desc}. Say 'undo that' if it is wrong.", False
    except Exception as e:
        print(f"[calendar] edit failed: {e}", flush=True)
        return "The calendar did not accept that change. Please check the logs.", False

def undo_last_event() -> str:
    """Reverse the most recent add or edit."""
    global LAST_EVENT_ID, LAST_EDIT, CALENDAR_TEXT
    import requests
    try:
        creds = _google_credentials()
        headers = {"Authorization": f"Bearer {creds.token}"}
        if LAST_EDIT:
            eid, previous = LAST_EDIT
            r = requests.patch(f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{eid}", headers=headers, json=previous, timeout=10)
            r.raise_for_status()
            print(f"[calendar] reverted edit on {eid}: {previous}", flush=True)
            LAST_EDIT = None
            CALENDAR_TEXT = calendar_summary()
            return "Reverted that change."
        if LAST_EVENT_ID:
            r = requests.delete(f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{LAST_EVENT_ID}", headers=headers, timeout=10)
            r.raise_for_status()
            print(f"[calendar] removed {LAST_EVENT_ID}", flush=True)
            LAST_EVENT_ID = ""
            CALENDAR_TEXT = calendar_summary()
            return "Removed."
        return "There is nothing to undo."
    except Exception as e:
        print(f"[calendar] undo failed: {e}", flush=True)
        return "I could not undo that. Please check the logs."

# ---------- Persona ----------
# Edit CORE_INSTRUCTIONS to change who Aion is. Put facts about yourself in config/profile.md
# (copy config/profile.example.md), never in this file, so the code stays shareable.
# {address} is filled from AION_USER_NAME at launch.
CORE_INSTRUCTIONS = """
You are AION, a realtime voice assistant that lives on the user's desktop as an animated avatar. {address}

Core mission
- Detect intent fast. Confirm constraints in one short line. Act or summarize clearly. Offer two or three next steps, never a monologue.
- When you hear "Thank you Aion, that is all for now," acknowledge once and fall silent.

Voice and etiquette
- Warm, upbeat, plain English. No code switching unless asked.
- Speak a little above your natural pitch, with a bright, lively tone.
- Short sentences. Vary rhythm slightly. Smile in the voice. No filler.
- Keep replies to 1 to 3 sentences by default. Expand only when asked.
- If something is uncertain, say so and name what you would check next.
- Science supports, indicates, or is consistent with. Science never proves.

How to address the user
- {address}
- If the profile lists preferred phrases or transitions, use them sparingly.

Values and safety
- Prioritize accuracy, critical thinking, empathy, and media literacy.
- No medical, legal, or investment overreach. Provide sources or verification steps when stakes are high.
- Treat personal info as private. Do not volunteer names, dates, or contacts unless asked.

What you can and cannot do
- You have no backend model, no tools, no web access, and no way to look anything up. Never say "let me check", "let me think this through", "hang on", or anything that promises a lookup. Answer at once from what you know; if you do not know, say so in one sentence and move on.
- Your memory is the "Long-term memory" section below plus this conversation. Nothing else carries over between launches.
- When the user asks you to remember something, the program saves the user's words to the memory file and speaks a short confirmation for you. Do not add your own promise to remember; reply to the content only if there is something useful to say, otherwise stay quiet.
- If an "Upcoming calendar" section is present, it was read from the user's Google Calendar and is accurate as of the time shown. Use it when the user asks what is coming up. Always say the day before the event ("on Sunday at 2 PM, Team Meeting"), and never call a later day's event "coming up today". If the section is absent, say the calendar was not available at launch.
- When the user asks you to add, put, post, schedule, or block something on the calendar, the program creates the event and speaks the confirmation for you. Do not say "sure" or promise to do it, and do not ask follow-up questions; stay silent and let the confirmation come. If the user says "undo that", the program removes the last event it added. New events automatically take the color of earlier events with the same title.
- When the user asks to change, rename, move, or recolor an event, the program finds the event, applies the change, and speaks the confirmation for you. Stay silent and let it come. If the program asks for a detail, the user's next sentence completes the request. "Undo that" reverses the last add or edit.

Session behavior
- You can hear the user while you speak. If the user starts talking, stop and listen.
- Be patient with short pauses. Do not rush to fill silence.
- After the end phrase, remain silent until reactivated.
- If the user laughs or says "nice" or "got it," acknowledge once, then continue or end.
"""

MODE_PRESETS: Dict[str, str] = {
    "default": """
Mode: Default
- Keep replies short and option focused.
- If a task is procedural, give one step and wait.
- When scope is unclear, offer a choice: a two minute summary or step by step.
- Use one friendly opener occasionally, never every turn.
""",
    "lecture": """
Mode: Lecture Assist
- Boil down complex topics into simple units.
- Give one clear example and one surprising detail.
- Propose one slide learning objective and one quick learning check when relevant.
""",
    "grading": """
Mode: Grading
- Feedback is brief and specific. If most answers are correct, respond with one encouraging line.
- Do not reference internal keys. Flag only incorrect or unclear parts and suggest the minimum fix.
""",
    "job_search": """
Mode: Job Search
- Never embellish. Map skills truthfully to requirements in short spoken points.
- End with one smart question to ask the recruiter.
""",
    "tech_support": """
Mode: Tech Support
- Confirm system state and constraints first in one line.
- Give a minimal reproducible step set.
- Offer one likely cause and one quick diagnostic. Provide a safe fallback if the fix fails.
""",
}

def load_profile() -> str:
    """Your personal profile (config/profile.md or a JSON file), added to the instructions. Empty if absent."""
    if not PROFILE_PATH.exists():
        return ""
    try:
        text = PROFILE_PATH.read_text(encoding="utf-8").strip()
    except Exception as e:
        print(f"[profile] could not read {PROFILE_PATH}: {e}", flush=True)
        return ""
    if PROFILE_PATH.suffix.lower() != ".json":
        return ("Personal profile for context\n" + text) if text else ""
    try:
        data = json.loads(text)
        extras = []
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, (str, int, float)):
                    extras.append(f"- {k}: {v}")
                elif isinstance(v, list):
                    extras.append(f"- {k}: {', '.join(map(str, v))}")
                elif isinstance(v, dict):
                    extras.append(f"- {k}: " + "; ".join(f"{kk}={vv}" for kk, vv in v.items()))
        if extras:
            return "Personal profile for context\n" + "\n".join(extras) + "\n"
    except Exception:
        pass
    return ""

def address_rule() -> str:
    if USER_NAME:
        return f'Address the user as "{USER_NAME}" unless the user asks for something else.'
    return "No name is configured. Early in the first conversation, ask once what the user would like to be called."

def normalize_mode(mode: str) -> str:
    key = mode.lower().strip().replace(" ", "_")
    return key if key in MODE_PRESETS else "default"

def build_instructions(mode: str) -> str:
    parts = [
        CORE_INSTRUCTIONS.strip().replace("{address}", address_rule()),
        load_profile(),
        MODE_PRESETS[normalize_mode(mode)].strip(),
    ]
    mem = load_memory()
    if mem:
        parts.append("Long-term memory (notes the user keeps for you; treat as true unless the user corrects you)\n" + mem)
    if CALENDAR_TEXT:
        import datetime
        parts.append(f"Upcoming calendar (read at launch, {datetime.datetime.now().strftime('%A %I:%M %p').replace(' 0', ' ')})\n" + CALENDAR_TEXT)
    return "\n\n".join(p for p in parts if p).strip()

CURRENT_MODE = normalize_mode(os.getenv("AURORA_MODE", "default"))

# ---------- API auth ----------
def load_api_key() -> str:
    key = os.getenv("OPENAI_API_KEY")
    if key and key.strip():
        key = key.strip()
        print(f"[auth] key from environment variable OPENAI_API_KEY (...{key[-4:]}, {len(key)} chars)", flush=True)
        return key
    here = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    for key_path in (here / "keys" / "openai.key", here.parent / "keys" / "openai.key"):
        if key_path.exists():
            key = key_path.read_text(encoding="utf-8").strip()
            print(f"[auth] key from {key_path} (...{key[-4:]}, {len(key)} chars)", flush=True)
            return key
    print("ERROR: No API key found. Set OPENAI_API_KEY or place it in keys/openai.key", flush=True)
    sys.exit(1)

OPENAI_API_KEY = load_api_key()
HEADERS = {"Authorization": f"Bearer {OPENAI_API_KEY}", "User-Agent": USER_AGENT}

# ---------- Audio device selection ----------
# MME first: it resamples to the device rate for us. WASAPI shared mode rejects 24 kHz on 48 kHz endpoints
# ("Invalid sample rate", PaErrorCode -9997). WDM-KS is deliberately excluded (no blocking/callback mix support).
HOSTAPI_PREFERENCE = ["mme", "windows directsound", "windows wasapi"]

def hostapi_name(dev: dict) -> str:
    try:
        return sd.query_hostapis(dev["hostapi"])["name"].lower()
    except Exception:
        return ""

def hostapi_rank(dev: dict) -> int:
    name = hostapi_name(dev)
    for i, pref in enumerate(HOSTAPI_PREFERENCE):
        if pref in name:
            return i
    return 99   # unknown or WDM-KS: never auto-selected

def list_devices():
    print("\n=== Available Audio Devices ===", flush=True)
    devices = sd.query_devices()
    for idx, d in enumerate(devices):
        caps = []
        if d.get("max_input_channels", 0) > 0:
            caps.append("IN")
        if d.get("max_output_channels", 0) > 0:
            caps.append("OUT")
        print(f"[{idx}] {d['name']}  ({'/'.join(caps)})  [{hostapi_name(d)}]", flush=True)
    print("================================\n", flush=True)
    return devices

def pick_device_index(kind: str, keywords: List[str]) -> Optional[int]:
    """Best device whose name contains one of the keywords, preferring WASAPI > MME > DirectSound.
    Keyword order matters: an earlier keyword beats a later one."""
    devices = sd.query_devices()
    best = None
    best_score = None
    for idx, d in enumerate(devices):
        if kind == "input" and d.get("max_input_channels", 0) < 1:
            continue
        if kind == "output" and d.get("max_output_channels", 0) < 1:
            continue
        rank = hostapi_rank(d)
        if rank >= 99:
            continue
        name = d["name"].lower()
        for kw_i, kw in enumerate(keywords):
            if kw.lower() in name:
                score = (kw_i, rank)
                if best_score is None or score < best_score:
                    best, best_score = idx, score
                break
    if best is not None:
        print(f"[audio] Using {kind} device: [{best}] {devices[best]['name']} [{hostapi_name(devices[best])}]", flush=True)
    return best

def default_output_index() -> Optional[int]:
    """Windows default output, unless it is on an excluded host API; then the best-ranked output."""
    devices = sd.query_devices()
    try:
        d = sd.default.device[1]
        if d is not None and d >= 0 and hostapi_rank(devices[d]) < 99:
            print(f"[audio] Using default output: [{d}] {devices[d]['name']}", flush=True)
            return int(d)
    except Exception:
        pass
    ranked = [(hostapi_rank(d), i) for i, d in enumerate(devices) if d.get("max_output_channels", 0) > 0 and hostapi_rank(d) < 99]
    if ranked:
        ranked.sort()
        i = ranked[0][1]
        print(f"[audio] Using first usable output: [{i}] {devices[i]['name']}", flush=True)
        return i
    return None

def env_keywords(name: str, default: str) -> List[str]:
    """Comma-separated device-name keywords from the environment; earlier keywords win."""
    return [k.strip().lower() for k in os.getenv(name, default).split(",") if k.strip()]

def env_index(name: str) -> Optional[int]:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return None
    try:
        return int(v)
    except ValueError:
        print(f"[audio] Ignoring {name}={v!r} (not an integer)", flush=True)
        return None

# ---------- Playback (callback driven, non-blocking) ----------
class Playback:
    """Fan out mono PCM16 to one or more output devices from a shared buffer.
    write() only appends bytes; the PortAudio callbacks drain it, so the asyncio loop never blocks."""

    def __init__(self, device_indices: List[int]):
        self._lock = threading.Lock()
        self._buffers: List[bytearray] = []
        self.streams: List[sd.OutputStream] = []
        for dev in device_indices:
            info = sd.query_devices(dev)
            ch = 2 if info.get("max_output_channels", 0) >= 2 else 1
            buf = bytearray()
            self._buffers.append(buf)
            stream = None
            rates = [SAMPLE_RATE, int(info.get("default_samplerate") or 48000), 48000, 44100]
            last_err = None
            for rate in dict.fromkeys(rates):          # unique, in order
                try:
                    stream = sd.OutputStream(
                        samplerate=rate, channels=ch, dtype="int16", device=dev,
                        blocksize=int(PLAYBACK_BLOCK * rate / SAMPLE_RATE),
                        callback=self._make_callback(buf, ch, rate),
                    )
                    stream.start()
                    break
                except Exception as e:
                    last_err = e
                    stream = None
            if stream is None:
                raise last_err
            self.streams.append(stream)
            note = "" if rate == SAMPLE_RATE else f", resampling {SAMPLE_RATE} -> {rate} Hz"
            print(f"[audio] Output stream open on [{dev}] {info['name']} ({ch} ch{note})", flush=True)
        if os.getenv("AURORA_STARTUP_BEEP") == "1":
            self._beep()

    def _make_callback(self, buf: bytearray, ch: int, rate: int):
        ratio = SAMPLE_RATE / rate                     # source samples per output sample
        def cb(outdata, frames, time_info, status):
            src_frames = frames if rate == SAMPLE_RATE else int(round(frames * ratio))
            need = src_frames * 2                      # bytes of mono int16 at 24 kHz
            with self._lock:
                chunk = bytes(buf[:need])
                del buf[:need]
            if len(chunk) < need:
                chunk += b"\x00" * (need - len(chunk))
            mono = np.frombuffer(chunk, dtype=np.int16)
            if rate != SAMPLE_RATE:                    # linear resample to the device rate
                x_src = np.linspace(0.0, 1.0, num=len(mono), endpoint=False)
                x_dst = np.linspace(0.0, 1.0, num=frames, endpoint=False)
                mono = np.interp(x_dst, x_src, mono.astype(np.float32)).astype(np.int16)
            mono = mono.reshape(-1, 1)
            outdata[:] = mono if ch == 1 else np.repeat(mono, 2, axis=1)
        return cb

    def write(self, pcm: bytes):
        if not pcm:
            return
        if len(pcm) % 2:
            pcm = pcm[:-1]
        with self._lock:
            for buf in self._buffers:
                buf.extend(pcm)

    def clear(self):
        with self._lock:
            for buf in self._buffers:
                del buf[:]

    def queued_ms(self) -> float:
        with self._lock:
            n = len(self._buffers[0]) if self._buffers else 0
        return n / 2 / SAMPLE_RATE * 1000.0

    def _beep(self, freq=880.0, ms=200):
        t = np.linspace(0, ms / 1000.0, int(SAMPLE_RATE * ms / 1000.0), False)
        wave = (0.2 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
        self.write((np.clip(wave, -1, 1) * 32767).astype(np.int16).tobytes())

    def close(self):
        for s in self.streams:
            try:
                s.stop(); s.close()
            except Exception:
                pass

# ---------- Microphone ----------
def start_microphone(queue: asyncio.Queue, loop: asyncio.AbstractEventLoop, input_device: Optional[int]) -> sd.InputStream:
    def put(b64: str):
        if queue.full():
            try:
                queue.get_nowait()          # drop the oldest chunk rather than raise
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait(b64)

    def make_callback(rate: int):
        def callback(indata, frames, time_info, status):
            mono = indata[:, 0]
            if rate != SAMPLE_RATE:                # linear resample down/up to 24 kHz
                n_out = int(round(frames * SAMPLE_RATE / rate))
                x_src = np.linspace(0.0, 1.0, num=frames, endpoint=False)
                x_dst = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
                mono = np.interp(x_dst, x_src, mono.astype(np.float32)).astype(np.int16)
            loop.call_soon_threadsafe(put, base64.b64encode(mono.tobytes()).decode("ascii"))
        return callback

    info = sd.query_devices(input_device) if input_device is not None else sd.query_devices(kind="input")
    rates = [SAMPLE_RATE, int(info.get("default_samplerate") or 48000), 48000, 44100, 16000]
    last_err = None
    for rate in dict.fromkeys(rates):
        try:
            stream = sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                                    blocksize=int(CHUNK_SAMPLES * rate / SAMPLE_RATE),
                                    callback=make_callback(rate), device=input_device)
            stream.start()
            note = "" if rate == SAMPLE_RATE else f" (capturing at {rate} Hz, resampling to {SAMPLE_RATE})"
            print(f"[audio] Mic stream open on [{input_device}] {info['name']}{note}", flush=True)
            return stream
        except Exception as e:
            last_err = e
    raise last_err

# ---------- Live API helpers ----------
def session_start_payload(mode: str) -> dict:
    session = {
        "model": MODEL,
        "instructions": build_instructions(mode),
        "audio": {
            "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
            "output": {"voice": VOICE},
        },
    }
    if BACKEND_MODEL:
        session["delegation"] = {
            "type": "responses",
            "responses": {"model": BACKEND_MODEL, "instructions": build_instructions(mode)},
        }
    return {"type": "session.start", "session": session}

async def say(ws, text: str):
    """Ask Aion to speak this content now."""
    await ws.send(json.dumps({"type": "session.commentary.append", "delegation_id": None, "content": text}))

async def add_instructions(ws, text: str):
    await ws.send(json.dumps({"type": "session.instructions.append", "delegation_id": None, "content": text}))

class State:
    def __init__(self):
        self.ready = asyncio.Event()
        self.current_mode = CURRENT_MODE
        self.ended = False
        self.user_buffer: List[str] = []
        self.handled_utterance = False
        self.speaker = None            # who the console is currently printing for: "aion" | "you" | None
        self.last_input_ts = 0.0       # loop time of the last input transcript delta
        self.pending_calendar = ""     # a calendar request that still needs details from the next sentence
        self.pending_edit = ""         # same, for a change to an existing event
        self.seen_types = set()

    def console(self, who: str, text: str):
        """Print transcript text, tagging the speaker only when the speaker changes."""
        if who != self.speaker:
            if self.speaker is not None:
                print("", flush=True)
            print(f"[{who}] ", end="", flush=True)
            self.speaker = who
        print(text, end="", flush=True)

    def reset_utterance(self):
        self.user_buffer = []
        self.handled_utterance = False

    def user_text(self) -> str:
        return "".join(self.user_buffer).strip().lower()

async def handle_user_commands(ws, state: State, final: bool = False) -> bool:
    """final=True means the transcript for this utterance is complete (needed for commands that save text)."""
    text = state.user_text()
    if not text or state.handled_utterance:
        return False

    if re.search(r"thank you,?\s+(aion|aurora)", text) and re.search(r"that('s| is) all for now", text):
        state.handled_utterance = True
        await say(ws, "Understood. Standing by.")
        state.ended = True
        return True

    # "remember that X", "I would like you to remember that X", "make a note that X", "aion, remember X"
    m = (re.match(r"^\W*(?:(?:hey|ok|okay)?\s*(?:aion|aurora),?\s*)?(?:please\s+)?remember\s+(?:that\s+)?(.{4,})$", text)
         or re.search(r"\b(?:remember|make a note|note|write down|keep in mind)\s+that\s+(.{4,})$", text))
    if m and final and not re.search(r"\b(?:do|did|can|could|will|would) you (?:still )?remember\b|\bremember (?:when|how|what|where|who|why|if)\b", text):
        state.handled_utterance = True
        note = m.group(1).strip()
        if remember(note):
            await add_instructions(ws, f"New long-term memory note from the user: {note}")
            await say(ws, "Noted, I will keep that.")
        else:
            await say(ws, "I could not write to the memory file. Please check the logs.")
        return True

    is_add = re.search(r"\b(?:add|put|post|schedule|create|block|book)\b.*\b(?:calendar|schedule)\b|\b(?:on|to|in) my calendar\b|^\W*(?:(?:hey\s+)?aion,?\s*)?(?:please\s+)?(?:schedule|book)\b", text) \
        and not re.search(r"\bwhat(?:'s| is)\b.*\bcalendar\b", text)
    if final and (is_add or state.pending_calendar):
        state.handled_utterance = True
        request = (state.pending_calendar + " " + state.user_text()).strip() if state.pending_calendar else state.user_text()
        print(f"[calendar] add request: {request}", flush=True)
        reply, needs_more = await asyncio.to_thread(add_calendar_event, request)
        state.pending_calendar = request if needs_more else ""
        if CALENDAR_TEXT:
            await add_instructions(ws, "Upcoming calendar (refreshed just now)\n" + CALENDAR_TEXT)
        await say(ws, reply)
        return True

    is_edit = re.search(r"\b(?:change|rename|move|reschedule|update|fix|correct|recolor|relabel|shift|push|extend|shorten)\b.*\b(?:event|calendar|entry|appointment|it|that|label|color|colour|title|name|time)\b", text) \
        and not re.search(r"\bwhat(?:'s| is)\b.*\bcalendar\b", text)
    # a correction right after an add or edit: "it should have said Sunday Market", "that should be 2 to 5"
    if not is_edit and LAST_EVENT_ID and re.search(r"\b(?:it|that|title|name|label|color|time)\b.*\bshould (?:have (?:been|said|used)|be|say|use)\b", text):
        is_edit = True
    if final and (is_edit or state.pending_edit):
        state.handled_utterance = True
        request = (state.pending_edit + " " + state.user_text()).strip() if state.pending_edit else state.user_text()
        print(f"[calendar] edit request: {request}", flush=True)
        reply, needs_more = await asyncio.to_thread(edit_calendar_event, request)
        state.pending_edit = request if needs_more else ""
        if CALENDAR_TEXT:
            await add_instructions(ws, "Upcoming calendar (refreshed just now)\n" + CALENDAR_TEXT)
        await say(ws, reply)
        return True

    if final and re.search(r"\bundo (?:that|it|the last one)\b", text):
        state.handled_utterance = True
        reply = await asyncio.to_thread(undo_last_event)
        if CALENDAR_TEXT:
            await add_instructions(ws, "Upcoming calendar (refreshed just now)\n" + CALENDAR_TEXT)
        await say(ws, reply)
        return True

    if re.search(r"\b(show|list)\s+modes\b", text):
        state.handled_utterance = True
        await say(ws, f"Available modes: {', '.join(sorted(MODE_PRESETS))}. Current mode is {state.current_mode}.")
        return True

    m = re.search(r"\b(?:set|switch|change)\s+mode\s+(?:to\s+)?([a-z_ ]+)", text) or re.search(r"\bmode\s+([a-z_ ]+)", text)
    if m:
        target = m.group(1).strip().replace(" ", "_")
        # trim trailing words the transcript may have added ("lecture please")
        for candidate in (target, target.split("_")[0]):
            if candidate in MODE_PRESETS:
                state.handled_utterance = True
                state.current_mode = candidate
                await add_instructions(ws, MODE_PRESETS[candidate].strip())
                await say(ws, f"Mode set to {candidate.replace('_', ' ')}.")
                return True
    return False

async def finish_utterance(ws, state: State):
    """The user's sentence is complete: run the commands that need the whole sentence, then reset."""
    if not state.user_buffer:
        return
    await handle_user_commands(ws, state, final=True)
    state.reset_utterance()
    if state.ended:
        await asyncio.sleep(2.5)                 # let "Standing by." finish playing
        try:
            await ws.send(json.dumps({"type": "session.close"}))
        except Exception:
            pass

async def utterance_watchdog(ws, state: State, silence_s: float = 2.0):
    """The Live API streams the user's transcript as deltas; a pause of silence_s marks the end of the sentence."""
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(0.25)
        if state.user_buffer and state.last_input_ts and loop.time() - state.last_input_ts >= silence_s:
            await finish_utterance(ws, state)

# ---------- Tasks ----------
async def sender(ws, mic_q: asyncio.Queue, state: State):
    await state.ready.wait()
    while True:                                  # runs until cancelled by run_session
        b64 = await mic_q.get()
        await ws.send(json.dumps({"type": "session.input_audio.append", "audio": b64}))

async def receiver(ws, playback: Playback, state: State):
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        typ = msg.get("type", "")

        if typ == "session.started":
            sid = (msg.get("session") or {}).get("id", "?")
            print(f"[aion] session started id={sid} model={MODEL} voice={VOICE} rate={SAMPLE_RATE}", flush=True)
            state.ready.set()
            prompt = boot_prompt()
            if prompt:
                await asyncio.sleep(0.2)
                print(f"[aion] boot prompt -> {prompt}", flush=True)
                await say(ws, prompt)

        elif typ == "session.output_audio.delta":
            b64 = msg.get("delta") or msg.get("audio")
            if b64:
                try:
                    playback.write(base64.b64decode(b64))
                except Exception:
                    pass

        elif typ == "session.output_transcript.delta":
            state.console("aion", msg.get("delta", ""))
        elif typ in ("session.output_transcript.done", "session.output_transcript.completed"):
            pass

        elif typ == "session.input_transcript.delta":
            delta = msg.get("delta", "")
            if delta:
                state.user_buffer.append(delta)
                state.last_input_ts = asyncio.get_running_loop().time()
                state.console("you", delta)
                await handle_user_commands(ws, state)
        elif typ in ("session.input_transcript.done", "session.input_transcript.completed"):
            await finish_utterance(ws, state)
            if state.ended:
                break

        elif "clear" in typ or "interrupt" in typ or "truncate" in typ:
            playback.clear()                     # server cut the response; drop buffered audio
            print(f"[aion] ({typ}) playback cleared", flush=True)

        elif typ == "session.closed":
            usage = msg.get("usage") or msg.get("session", {}).get("usage")
            print(f"[aion] session closed. usage={usage}", flush=True)
            state.ended = True
            break

        elif typ == "error":
            print(f"[Live error] {msg.get('error', msg)}", flush=True)

        elif typ == "session.delegation.created":
            # She decided to hand the question to a backend. There is none, so answer the delegation
            # right away and tell her to reply from what she already has.
            d = msg.get("delegation") or {}
            did = d.get("id") or msg.get("delegation_id") or msg.get("id")
            print(f"\n[aion] delegation requested (id={did}); no backend, closing it", flush=True)
            await ws.send(json.dumps({
                "type": "session.commentary.append", "delegation_id": did,
                "content": "There is no backend, tool, or lookup available. Answer now, in one or two sentences, "
                           "from the conversation and your instructions. Do not say you are checking or lining anything up.",
            }))

        elif typ in ("response.event", "session.usage.updated", "session.commentary.appended",
                     "session.instructions.appended", "session.thinking.appended"):
            pass                                 # bookkeeping acknowledgements; nothing to do

        elif typ not in state.seen_types:
            state.seen_types.add(typ)
            print(f"\n[aion] (unhandled event type: {typ})", flush=True)

async def run_session(playback: Playback, mic_q: asyncio.Queue, state: State):
    print(f"Connecting to {WS_URL} ({MODEL})", flush=True)
    kwargs = dict(ping_interval=20, ping_timeout=60, close_timeout=5, max_queue=None, max_size=None)
    try:
        major = int(str(websockets.__version__).split(".")[0])
    except Exception:
        major = 14
    if major >= 14:
        ws_ctx = websockets.connect(WS_URL, additional_headers=HEADERS, **kwargs)
    else:
        ws_ctx = websockets.connect(WS_URL, extra_headers=HEADERS, **kwargs)     # websockets <= 13

    async with ws_ctx as ws:
        await ws.send(json.dumps(session_start_payload(state.current_mode)))
        tasks = [
            asyncio.create_task(sender(ws, mic_q, state)),
            asyncio.create_task(receiver(ws, playback, state)),
            asyncio.create_task(utterance_watchdog(ws, state)),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            exc = t.exception()
            if exc:
                raise exc

async def main():
    global CALENDAR_TEXT
    list_devices()
    CALENDAR_TEXT = await asyncio.to_thread(calendar_summary)
    if load_memory():
        print(f"[memory] loaded {MEMORY_PATH}", flush=True)
    else:
        print(f"[memory] no notes yet at {MEMORY_PATH}", flush=True)

    input_device = env_index("AURORA_IN_DEV")
    if input_device is None:
        input_device = pick_device_index("input", env_keywords("AURORA_MIC_KEYWORDS", "microphone"))

    vb_device = env_index("AURORA_OUT_VB")
    if vb_device is None:
        vb_device = pick_device_index("output", ["cable input"])
    if vb_device is None:
        print("[audio] WARNING: VB-CABLE 'CABLE Input' not found. VMagicMirror will not lip sync. "
              "Install VB-CABLE from vb-audio.com or set AURORA_OUT_VB.", flush=True)

    outputs: List[int] = [d for d in [vb_device] if d is not None]
    if os.getenv("AURORA_NO_MONITOR") != "1":
        mon = env_index("AURORA_OUT_MON")
        if mon is None:
            mon = pick_device_index("output", env_keywords("AURORA_MONITOR_KEYWORDS", "headphones,speakers"))
        if mon is None:
            mon = default_output_index()
        if mon is not None and mon not in outputs:
            outputs.append(mon)
    if not outputs:
        print("ERROR: no usable output device.", flush=True)
        sys.exit(2)

    playback = Playback(outputs)
    loop = asyncio.get_running_loop()
    mic_q: asyncio.Queue = asyncio.Queue(maxsize=MIC_QUEUE_MAX)
    mic_stream = start_microphone(mic_q, loop, input_device)
    state = State()

    try:
        backoff = 3
        while not state.ended:
            try:
                await run_session(playback, mic_q, state)
                if not state.ended:
                    print("[ws] connection closed by server; reconnecting...", flush=True)
            except (websockets.exceptions.ConnectionClosed, asyncio.TimeoutError, OSError) as e:
                print(f"[ws] disconnected: {e}. Reconnecting in {backoff}s...", flush=True)
            except websockets.exceptions.InvalidStatus as e:
                print(f"[ws] server refused the connection: {e}. Check the API key and model access.", flush=True)
                break
            if state.ended:
                break
            state.ready.clear()
            playback.clear()
            while not mic_q.empty():
                mic_q.get_nowait()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
    finally:
        try:
            mic_stream.stop(); mic_stream.close()
        except Exception:
            pass
        playback.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nAion session ended.", flush=True)

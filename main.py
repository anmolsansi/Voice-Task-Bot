from fastapi import FastAPI, Request
import os
import json
import requests
from dotenv import load_dotenv
import dateparser
from datetime import datetime, time, timedelta
import pytz

from db import SessionLocal, Task, init_db

# ----------------------------
# Startup / env
# ----------------------------
load_dotenv()
print("[DEBUG] .env file loaded")

init_db()
print("[DEBUG] Database initialized")

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_USER_ID = os.getenv("SLACK_USER_ID")
TIMEZONE = os.getenv("TIMEZONE", "America/Chicago")
USE_OLLAMA = os.getenv("USE_OLLAMA", "1")  # "1" or "0"
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "mistral:latest")

print(f"[DEBUG] SLACK_BOT_TOKEN loaded: {bool(SLACK_BOT_TOKEN)}")
print(f"[DEBUG] SLACK_USER_ID: {SLACK_USER_ID}")
print(f"[DEBUG] TIMEZONE: {TIMEZONE}")
print(f"[DEBUG] USE_OLLAMA: {USE_OLLAMA}")
print(f"[DEBUG] OLLAMA_MODEL: {OLLAMA_MODEL}")

tz = pytz.timezone(TIMEZONE)

app = FastAPI()


# ----------------------------
# Slack helpers
# ----------------------------
def slack_post_message(text: str):
    print(f"[DEBUG] Posting to Slack: {text}")
    url = "https://slack.com/api/chat.postMessage"
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-type": "application/json",
    }
    payload = {"channel": SLACK_USER_ID, "text": text}
    r = requests.post(url, headers=headers, json=payload)
    try:
        # print(f"[DEBUG] Slack response: {r.json()}")
        return r.json()
    except Exception:
        print("[DEBUG] Slack response not JSON:", r.text)
        return {"ok": False, "error": "non_json_response"}


def slack_schedule_message(text: str, post_at: int):
    print(f"[DEBUG] Scheduling message: '{text}' at {post_at}")
    url = "https://slack.com/api/chat.scheduleMessage"
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-type": "application/json",
    }
    payload = {"channel": SLACK_USER_ID, "text": text, "post_at": post_at}
    r = requests.post(url, headers=headers, json=payload)
    try:
        print(f"[DEBUG] Slack schedule response:") #{r.json()}")
        return r.json()
    except Exception:
        print("[DEBUG] Slack schedule response not JSON:", r.text)
        return {"ok": False, "error": "non_json_response"}


# ----------------------------
# Reminder schedule defaults
# ----------------------------
def reminder_times():
    """Default 5x/day reminder times."""
    return [
        time(10, 0),
        time(13, 0),
        time(15, 0),
        time(18, 0),
        time(20, 0),
    ]


# ----------------------------
# Date parsing fallback (dateparser)
# ----------------------------
def extract_datetime_from_text(text: str):
    """
    Returns (date, is_range) where date is a datetime.date
    and is_range tells you if it looked like a period (week, month, etc.)
    """
    settings = {
        "TIMEZONE": TIMEZONE,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
    }

    lower = text.lower()

    # special phrases for multi-day ranges (minimal support in fallback)
    if "every day this week" in lower or "daily this week" in lower:
        dt = dateparser.parse("tomorrow", settings=settings)
        if dt is None:
            dt = datetime.now(tz) + timedelta(days=1)
        return dt.date(), True

    dt = dateparser.parse(lower, settings=settings)
    if dt is None:
        dt = datetime.now(tz) + timedelta(days=1)

    return dt.date(), False


# ----------------------------
# Weekend helpers (fallback)
# ----------------------------
def is_weekend_phrase(text: str) -> bool:
    t = text.lower()
    return ("weekend" in t) or ("on the weekend" in t) or ("this weekend" in t) or ("next weekend" in t)


def upcoming_weekend_dates(now_dt: datetime):
    """
    Returns [sat_date, sun_date] for the next weekend.
    If today is Sat -> returns [today, tomorrow]
    If today is Sun -> returns [next Sat, next Sun]
    """
    today = now_dt.date()
    wd = now_dt.weekday()  # Mon=0 ... Sun=6

    # Saturday
    if wd == 5:
        sat = today
        sun = today + timedelta(days=1)
        return [sat, sun]

    # Sunday -> next weekend
    if wd == 6:
        sat = today + timedelta(days=6)
        sun = sat + timedelta(days=1)
        return [sat, sun]

    # Mon-Fri -> upcoming Saturday
    days_until_sat = 5 - wd
    sat = today + timedelta(days=days_until_sat)
    sun = sat + timedelta(days=1)
    return [sat, sun]


def next_weekend_dates(now_dt: datetime):
    """
    Returns [sat_date, sun_date] for the weekend AFTER the upcoming one.
    """
    upcoming = upcoming_weekend_dates(now_dt)
    sat = upcoming[0] + timedelta(days=7)
    return [sat, sat + timedelta(days=1)]


def ollama_is_up(timeout: float = 0.6) -> bool:
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=timeout)
        return r.status_code == 200
    except requests.exceptions.RequestException:
        return False


# ----------------------------
# DB context for Ollama
# ----------------------------
def fetch_recent_tasks_context(limit: int = 20) -> str:
    session = SessionLocal()
    try:
        rows = (
            session.query(Task)
            .filter(Task.completed == False)
            .order_by(Task.date.desc())
            .limit(limit)
            .all()
        )
        items = []
        for r in rows:
            items.append(
                {
                    "id": r.id,
                    "task": r.task,
                    "date": str(r.date),
                    "times": r.times_csv,
                    "completed": r.completed,
                }
            )
        return json.dumps(items)
    finally:
        session.close()


# ----------------------------
# Ollama parser (preferred when available)
# ----------------------------
def parse_with_ollama(text: str, now_iso: str, db_context_json: str):
    """
    Returns dict {"task": str, "dates": [YYYY-MM-DD,...], "times": ["HH:MM",...]} or None.
    times is optional; if missing, use default reminder_times().
    """
    if os.getenv("USE_OLLAMA", "1") != "1":
        print("[DEBUG] USE_OLLAMA disabled, skipping Ollama")
        return None

    prompt = f"""
You are a strict JSON parser.

Current date/time (local): {now_iso}
Timezone: {TIMEZONE}

Here are existing pending tasks from the database (JSON):
{db_context_json}

Given the user sentence, extract:
- "task": the core task (remove date words)
- "dates": list of calendar dates the reminders should happen, format YYYY-MM-DD
- "times": optional list of reminder times in 24h format HH:MM (if user says "5 times", you may output 5 times; otherwise omit)

Rules:
- If user says "weekend" / "this weekend" / "on weekend", return ONLY the upcoming Saturday and Sunday dates.
- If user says "next weekend", return the Saturday+Sunday of the weekend AFTER the upcoming one.
- Do NOT output dates in the past.
- Prefer not to duplicate tasks already in DB: if same task already exists for those dates, keep dates empty [].

Return ONLY JSON in this exact schema (no extra text):
{{
  "task": "string",
  "dates": ["YYYY-MM-DD", ...],
  "times": ["HH:MM", ...]
}}

User sentence: "{text}"
"""

    try:
        resp = requests.post(
            "http://localhost:11434/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=10,
        )
    except requests.exceptions.RequestException as e:
        print("[DEBUG] Ollama not reachable, falling back:", e)
        return None

    try:
        data = resp.json()
        raw = data.get("response", "").strip()
        print("[DEBUG] Ollama raw response:", raw)

        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1:
            print("[DEBUG] No JSON braces found in Ollama response")
            return None

        parsed = json.loads(raw[start:end+1])

        if not isinstance(parsed, dict):
            return None
        if "task" not in parsed or "dates" not in parsed:
            return None
        if not isinstance(parsed["dates"], list):
            return None
        if "times" in parsed and not isinstance(parsed["times"], list):
            return None

        return parsed
    except Exception as e:
        print("[DEBUG] Ollama parse error:", e)
        return None


# ----------------------------
# Main endpoint
# ----------------------------
@app.post("/add_task")
async def add_task(request: Request):
    print()
    print("[DEBUG] /add_task endpoint called")
    data = await request.json()
    raw_text = (data.get("text") or "").strip()

    if not raw_text:
        return {"ok": False, "error": "empty_text"}

    print(f"[DEBUG] Received task: {raw_text}")
    now_dt = datetime.now(pytz.timezone(TIMEZONE))

    # Build DB context for Ollama
    db_context = fetch_recent_tasks_context(limit=25)
    now_iso = now_dt.isoformat()

    # ---------- 1) Try OLLAMA FIRST ----------
    dates = []
    task_text = None
    is_range = False
    custom_times = None

    if USE_OLLAMA == "1" and not ollama_is_up():
        print("[DEBUG] Ollama looks down, skipping Ollama and using fallback")
        ollama_result = None
    else:
        ollama_result = parse_with_ollama(raw_text, now_iso=now_iso, db_context_json=db_context)
    if ollama_result:
        print("[DEBUG] Using Ollama result" + str(ollama_result))
        task_text = (ollama_result.get("task") or raw_text).strip()
        custom_times = ollama_result.get("times")

        for d in ollama_result.get("dates", []):
            try:
                dates.append(datetime.fromisoformat(d).date())
            except Exception as e:
                print("[DEBUG] Bad date from Ollama:", d, e)

        if dates:
            is_range = len(dates) > 1
        else:
            # Ollama chose to dedupe (dates empty)
            return {"ok": True, "skipped": True, "reason": "duplicate_or_no_dates", "task": task_text}

    # ---------- 2) FALLBACK ----------
    if not dates:
        print("[DEBUG] Using fallback parsing")

        # Weekend fallback: "on weekend"
        if is_weekend_phrase(raw_text):
            dates = upcoming_weekend_dates(now_dt)
            is_range = True
            if not task_text:
                task_text = raw_text.lower().replace("weekend", "").replace("on the", "").strip().capitalize() or raw_text
        else:
            target_date, is_range_dp = extract_datetime_from_text(raw_text)
            dates = [target_date]
            is_range = is_range or is_range_dp
            if not task_text:
                cleaned = raw_text
                for w in ["today", "tomorrow", "every day this week"]:
                    cleaned = cleaned.replace(w, "")
                task_text = cleaned.strip().capitalize() or raw_text

    # ---------- 3) Choose reminder times ----------
    reminder_list = reminder_times()

    # If Ollama provided custom times, use them
    if custom_times:
        tmp = []
        for ts in custom_times:
            try:
                hh, mm = ts.split(":")
                tmp.append(time(int(hh), int(mm)))
            except Exception:
                pass
        if tmp:
            reminder_list = tmp

    # ---------- DEDUPE CHECK ----------
    session = SessionLocal()
    try:
        existing = (
            session.query(Task)
            .filter(Task.completed == False)
            .filter(Task.task == task_text)
            .filter(Task.date.in_(dates))
            .all()
        )
        if existing:
            print("[DEBUG] Duplicate task found in DB, skipping scheduling")
            return {"ok": True, "skipped": True, "reason": "duplicate_in_db", "task": task_text, "dates": [str(d) for d in dates]}
    finally:
        session.close()

    # ---------- 5) Schedule Slack messages ----------
    print(f"[DEBUG] Scheduling {len(reminder_list)} reminders for {len(dates)} day(s)")
    for d in dates:
        for t in reminder_list:
            dt = tz.localize(datetime.combine(d, t))
            slack_schedule_message(
                f"Reminder: {task_text}",
                int(dt.timestamp()),
            )

    # ---------- 6) Save to DB ----------
    session = SessionLocal()
    try:
        times_csv = ",".join([t.strftime("%H:%M") for t in reminder_list])
        last_id = None
        for d in dates:
            db_task = Task(
                raw_text=raw_text,
                task=task_text,
                date=d,
                times_csv=times_csv,
                is_range=is_range,
                completed=False,
            )
            session.add(db_task)
            session.flush()
            last_id = db_task.id
        session.commit()
        print(f"[DEBUG] Tasks saved to DB, last id {last_id}")
    finally:
        session.close()

    total = len(reminder_list) * len(dates)
    return {
        "ok": True,
        "task": task_text,
        "dates": [str(d) for d in dates],
        "reminders_per_day": len(reminder_list),
        "total_reminders": total,
        "is_range": is_range,
    }


# ----------------------------
# List tasks
# ----------------------------
@app.get("/tasks")
def list_tasks():
    session = SessionLocal()
    try:
        tasks = session.query(Task).order_by(Task.date).all()
        return [
            {
                "id": t.id,
                "raw_text": t.raw_text,
                "task": t.task,
                "date": str(t.date),
                "times": t.times_csv.split(","),
                "is_range": t.is_range,
                "completed": t.completed,
            }
            for t in tasks
        ]
    finally:
        session.close()


# ----------------------------
# Mark done (optional but useful)
# ----------------------------
@app.post("/tasks/{task_id}/done")
def mark_done(task_id: int):
    session = SessionLocal()
    try:
        t = session.query(Task).filter(Task.id == task_id).first()
        if not t:
            return {"ok": False, "error": "not_found"}
        t.completed = True
        session.commit()
        return {"ok": True, "id": task_id, "completed": True}
    finally:
        session.close()

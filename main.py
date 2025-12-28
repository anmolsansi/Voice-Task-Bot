from fastapi import FastAPI, Request
import os
import json
import requests
from dotenv import load_dotenv
import dateparser
from datetime import datetime, time, timedelta
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.base import JobLookupError

from db import SessionLocal, Task, Reminder, init_db

# ----------------------------
# Startup / env
# ----------------------------
load_dotenv()
print("[DEBUG] .env file loaded")

init_db()
print("[DEBUG] Database initialized")

TIMEZONE = os.getenv("TIMEZONE", "America/Chicago")
USE_OLLAMA = os.getenv("USE_OLLAMA", "1")  # "1" or "0"
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "mistral:latest")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

print(f"[DEBUG] TIMEZONE: {TIMEZONE}")
print(f"[DEBUG] USE_OLLAMA: {USE_OLLAMA}")
print(f"[DEBUG] OLLAMA_MODEL: {OLLAMA_MODEL}")
print(f"[DEBUG] TELEGRAM_BOT_TOKEN loaded: {bool(TELEGRAM_BOT_TOKEN)}")
print(f"[DEBUG] TELEGRAM_CHAT_ID: {TELEGRAM_CHAT_ID}")

tz = pytz.timezone(TIMEZONE)

app = FastAPI()
scheduler = BackgroundScheduler(timezone=TIMEZONE)


# ----------------------------
# Telegram helper
# ----------------------------
def telegram_send_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    r = requests.post(url, json=payload, timeout=10)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "error": "non_json_response", "raw": r.text}


# ----------------------------
# Reminder schedule defaults
# ----------------------------
def reminder_times():
    """Default reminder times."""
    return [
        time(10, 0),
        time(13, 0),
        time(15, 0),
        time(18, 0),
        time(20, 0),
        time(21, 0),
    ]


# ----------------------------
# Date parsing fallback (dateparser)
# ----------------------------
def extract_datetime_from_text(text: str):
    """
    Returns (date, is_range) where date is a datetime.date
    """
    settings = {
        "TIMEZONE": TIMEZONE,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
    }

    lower = text.lower()

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
    """
    today = now_dt.date()
    wd = now_dt.weekday()  # Mon=0 ... Sun=6

    if wd == 5:  # Sat
        return [today, today + timedelta(days=1)]
    if wd == 6:  # Sun -> next weekend
        sat = today + timedelta(days=6)
        return [sat, sat + timedelta(days=1)]

    # Mon-Fri -> upcoming Sat
    days_until_sat = 5 - wd
    sat = today + timedelta(days=days_until_sat)
    return [sat, sat + timedelta(days=1)]


def next_weekend_dates(now_dt: datetime):
    upcoming = upcoming_weekend_dates(now_dt)
    sat = upcoming[0] + timedelta(days=7)
    return [sat, sat + timedelta(days=1)]


# ----------------------------
# Ollama availability + DB context
# ----------------------------
def ollama_is_up(timeout: float = 0.6) -> bool:
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=timeout)
        return r.status_code == 200
    except requests.exceptions.RequestException:
        return False


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


def parse_with_ollama(text: str, now_iso: str, db_context_json: str):
    """
    Returns dict {"task": str, "dates": [YYYY-MM-DD,...], "times": ["HH:MM",...]} or None.
    """
    if USE_OLLAMA != "1":
        return None

    prompt = f"""
You are a strict JSON parser.

Current date/time (local): {now_iso}
Timezone: {TIMEZONE}

Existing pending tasks from the database (JSON):
{db_context_json}

Extract:
- "task": core task (remove date words)
- "dates": list of YYYY-MM-DD
- "times": optional list of HH:MM

Rules:
- "weekend"/"this weekend"/"on weekend" => upcoming Sat+Sun
- "next weekend" => weekend after upcoming
- no past dates
- if same task already exists for those dates in DB, return dates: []

Return ONLY JSON:
{{
  "task": "string",
  "dates": ["YYYY-MM-DD", ...],
  "times": ["HH:MM", ...]
}}

Sentence: "{text}"
"""

    try:
        resp = requests.post(
            "http://localhost:11434/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=8,
        )
    except requests.exceptions.RequestException:
        return None

    try:
        data = resp.json()
        raw = (data.get("response") or "").strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1:
            return None
        parsed = json.loads(raw[start : end + 1])
        if not isinstance(parsed, dict):
            return None
        if "task" not in parsed or "dates" not in parsed:
            return None
        if not isinstance(parsed["dates"], list):
            return None
        if "times" in parsed and not isinstance(parsed["times"], list):
            return None
        return parsed
    except Exception:
        return None


# ----------------------------
# Option 3: exact jobs, no polling
# ----------------------------
def job_id_for_reminder(reminder_id: int) -> str:
    return f"reminder:{reminder_id}"


def send_reminder_job(reminder_id: int):
    """
    This runs at the exact scheduled datetime.
    It reads DB, sends Telegram, and marks reminder sent if successful.
    """
    session = SessionLocal()
    try:
        r = session.query(Reminder).filter(Reminder.id == reminder_id).first()
        if not r:
            return
        if r.sent:
            return

        task = session.query(Task).filter(Task.id == r.task_id).first()
        if not task or task.completed:
            r.sent = True
            session.commit()
            return

        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            # keep it unsent so it can be retried on next restart/reschedule
            return

        resp = telegram_send_message(f"Reminder: {task.task}")
        if resp.get("ok") is True:
            r.sent = True
            session.commit()
    finally:
        session.close()


def schedule_reminder_job(reminder_id: int, run_at: datetime):
    """
    Schedule a one-time APScheduler job for this reminder.
    If run_at is in the past, do nothing (it will be handled on startup reschedule if you want).
    """
    now = datetime.now(tz)
    if run_at <= now:
        return

    jid = job_id_for_reminder(reminder_id)

    # Replace existing job if present
    try:
        scheduler.remove_job(jid)
    except JobLookupError:
        pass

    scheduler.add_job(
        send_reminder_job,
        trigger="date",
        run_date=run_at,
        args=[reminder_id],
        id=jid,
        replace_existing=True,
        misfire_grace_time=3600,  # if Mac sleeps, still fire within 1 hour after wake
        coalesce=True,
        max_instances=1,
    )


def schedule_pending_reminders_from_db(limit: int = 500):
    """
    On startup, schedule all unsent reminders that are still in the future.
    """
    session = SessionLocal()
    try:
        now = datetime.now(tz)
        rows = (
            session.query(Reminder)
            .join(Task, Reminder.task_id == Task.id)
            .filter(Reminder.sent == False)
            .filter(Task.completed == False)
            .filter(Reminder.run_at > now)
            .order_by(Reminder.run_at.asc())
            .limit(limit)
            .all()
        )
        for r in rows:
            schedule_reminder_job(r.id, r.run_at)
        print(f"[DEBUG] Scheduled {len(rows)} pending reminder job(s) from DB")
    finally:
        session.close()


@app.on_event("startup")
def startup():
    scheduler.start()
    schedule_pending_reminders_from_db()


# ----------------------------
# Main endpoint
# ----------------------------
@app.post("/add_task")
async def add_task(request: Request):
    data = await request.json()
    raw_text = (data.get("text") or "").strip()
    if not raw_text:
        return {"ok": False, "error": "empty_text"}

    now_dt = datetime.now(tz)
    db_context = fetch_recent_tasks_context(limit=25)
    now_iso = now_dt.isoformat()

    dates = []
    task_text = None
    is_range = False
    custom_times = None

    # 1) Ollama first (if enabled and up)
    if USE_OLLAMA == "1" and ollama_is_up():
        ollama_result = parse_with_ollama(raw_text, now_iso=now_iso, db_context_json=db_context)
    else:
        ollama_result = None

    if ollama_result:
        task_text = (ollama_result.get("task") or raw_text).strip()
        custom_times = ollama_result.get("times")

        for d in ollama_result.get("dates", []):
            try:
                dates.append(datetime.fromisoformat(d).date())
            except Exception:
                pass

        if dates:
            is_range = len(dates) > 1
        else:
            return {"ok": True, "skipped": True, "reason": "duplicate_or_no_dates", "task": task_text}

    # 2) Fallback
    if not dates:
        if is_weekend_phrase(raw_text):
            if "next weekend" in raw_text.lower():
                dates = next_weekend_dates(now_dt)
            else:
                dates = upcoming_weekend_dates(now_dt)
            is_range = True
            if not task_text:
                tmp = raw_text.lower()
                for phrase in ["next weekend", "this weekend", "on the weekend", "on weekend", "weekend"]:
                    tmp = tmp.replace(phrase, "")
                task_text = tmp.strip().capitalize() or raw_text
        else:
            target_date, is_range_dp = extract_datetime_from_text(raw_text)
            dates = [target_date]
            is_range = is_range or is_range_dp
            if not task_text:
                cleaned = raw_text
                for w in ["today", "tomorrow", "every day this week"]:
                    cleaned = cleaned.replace(w, "")
                task_text = cleaned.strip().capitalize() or raw_text

    # 3) times
    reminder_list = reminder_times()
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

    # 4) dedupe tasks (task_text + date + incomplete)
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
            return {
                "ok": True,
                "skipped": True,
                "reason": "duplicate_in_db",
                "task": task_text,
                "dates": [str(d) for d in dates],
            }
    finally:
        session.close()

    # 5) save + create reminders + schedule exact jobs
    created_reminders = 0
    session = SessionLocal()
    try:
        times_csv = ",".join([t.strftime("%H:%M") for t in reminder_list])
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

            for t in reminder_list:
                run_at = tz.localize(datetime.combine(d, t))
                reminder = Reminder(task_id=db_task.id, run_at=run_at, sent=False)
                session.add(reminder)
                session.flush()

                # schedule exact job
                schedule_reminder_job(reminder.id, run_at)
                created_reminders += 1

        session.commit()
    finally:
        session.close()

    return {
        "ok": True,
        "task": task_text,
        "dates": [str(d) for d in dates],
        "reminders_per_day": len(reminder_list),
        "total_reminders": created_reminders,
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
# Mark done
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


# ----------------------------
# Telegram test
# ----------------------------
@app.get("/telegram_test")
def telegram_test():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {"ok": False, "error": "missing_telegram_env"}
    return telegram_send_message("Test message from your bot ✅")

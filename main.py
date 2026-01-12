from fastapi import FastAPI, Request
import os
import json
import logging
import requests
from dotenv import load_dotenv
import dateparser
from datetime import datetime, time, timedelta
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.base import JobLookupError

from db import SessionLocal, Task, Reminder, init_db
from google_calendar import create_calendar_event, list_upcoming_events

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voice_task_bot")

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
GOOGLE_SYNC_DAYS = int(os.getenv("GOOGLE_SYNC_DAYS", "7"))
GOOGLE_SYNC_INTERVAL_MINUTES = int(os.getenv("GOOGLE_SYNC_INTERVAL_MINUTES", "5"))

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
def log_exception(context: str):
    logger.exception(context)


def telegram_send_message(text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
        r = requests.post(url, json=payload, timeout=10)
        try:
            return r.json()
        except Exception:
            return {"ok": False, "error": "non_json_response", "raw": r.text}
    except Exception as exc:
        log_exception("telegram_send_message failed")
        return {"ok": False, "error": "telegram_send_failed", "detail": str(exc)}


# ----------------------------
# Reminder schedule defaults
# ----------------------------
def reminder_times():
    """Default reminder times."""
    try:
        return [
            time(10, 0),
            time(13, 0),
            time(15, 0),
            time(18, 0),
            time(20, 0),
            time(21, 0),
        ]
    except Exception:
        log_exception("reminder_times failed")
        return []


# ----------------------------
# Date parsing fallback (dateparser)
# ----------------------------
def extract_datetime_from_text(text: str):
    """
    Returns (datetime, is_range) where datetime is timezone-aware
    """
    try:
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
            return dt, True

        dt = dateparser.parse(lower, settings=settings)
        if dt is None:
            dt = datetime.now(tz) + timedelta(days=1)

        return dt, False
    except Exception:
        log_exception("extract_datetime_from_text failed")
        return datetime.now(tz) + timedelta(days=1), False


def has_explicit_time(dt: datetime) -> bool:
    try:
        return not (dt.hour == 0 and dt.minute == 0 and dt.second == 0 and dt.microsecond == 0)
    except Exception:
        log_exception("has_explicit_time failed")
        return False


def ensure_tzaware(dt: datetime) -> datetime:
    try:
        if dt.tzinfo is None:
            return tz.localize(dt)
        return dt
    except Exception:
        log_exception("ensure_tzaware failed")
        return dt


# ----------------------------
# Weekend helpers (fallback)
# ----------------------------
def is_weekend_phrase(text: str) -> bool:
    try:
        t = text.lower()
        return ("weekend" in t) or ("on the weekend" in t) or ("this weekend" in t) or ("next weekend" in t)
    except Exception:
        log_exception("is_weekend_phrase failed")
        return False


def upcoming_weekend_dates(now_dt: datetime):
    """
    Returns [sat_date, sun_date] for the next weekend.
    """
    try:
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
    except Exception:
        log_exception("upcoming_weekend_dates failed")
        today = datetime.now(tz).date()
        return [today, today + timedelta(days=1)]


def next_weekend_dates(now_dt: datetime):
    try:
        upcoming = upcoming_weekend_dates(now_dt)
        sat = upcoming[0] + timedelta(days=7)
        return [sat, sat + timedelta(days=1)]
    except Exception:
        log_exception("next_weekend_dates failed")
        today = datetime.now(tz).date()
        return [today, today + timedelta(days=1)]


# ----------------------------
# Ollama availability + DB context
# ----------------------------
def ollama_is_up(timeout: float = 0.6) -> bool:
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=timeout)
        return r.status_code == 200
    except requests.exceptions.RequestException:
        return False
    except Exception:
        log_exception("ollama_is_up failed")
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
    except Exception:
        log_exception("fetch_recent_tasks_context failed")
        return "[]"
    finally:
        session.close()


def parse_with_ollama(text: str, now_iso: str, db_context_json: str):
    """
    Returns dict {"task": str, "dates": [YYYY-MM-DD,...], "times": ["HH:MM",...], "start_at": iso} or None.
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
- "start_at": optional ISO datetime with timezone when an exact time is provided

Rules:
- "weekend"/"this weekend"/"on weekend" => upcoming Sat+Sun
- "next weekend" => weekend after upcoming
- no past dates
- if same task already exists for those dates in DB, return dates: []
- if an exact time is present, include "start_at"

Return ONLY JSON:
{{
  "task": "string",
  "dates": ["YYYY-MM-DD", ...],
  "times": ["HH:MM", ...],
  "start_at": "YYYY-MM-DDTHH:MM:SS-06:00"
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
    except Exception:
        log_exception("parse_with_ollama request failed")
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
        log_exception("parse_with_ollama parse failed")
        return None


# ----------------------------
# Option 3: exact jobs, no polling
# ----------------------------
def job_id_for_reminder(reminder_id: int) -> str:
    try:
        return f"reminder:{reminder_id}"
    except Exception:
        log_exception("job_id_for_reminder failed")
        return f"reminder:unknown"


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
    except Exception:
        log_exception("send_reminder_job failed")
    finally:
        session.close()


def schedule_reminder_job(reminder_id: int, run_at: datetime):
    """
    Schedule a one-time APScheduler job for this reminder.
    If run_at is in the past, do nothing (it will be handled on startup reschedule if you want).
    """
    try:
        run_at = ensure_tzaware(run_at)
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
    except Exception:
        log_exception("schedule_reminder_job failed")


def schedule_pending_reminders_from_db(limit: int = 500):
    """
    On startup, schedule all unsent reminders that are still in the future.
    """
    session = SessionLocal()
    try:
        now = datetime.now()
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
    except Exception:
        log_exception("schedule_pending_reminders_from_db failed")
    finally:
        session.close()


def sync_google_calendar_events():
    """
    Pull upcoming Google Calendar events and schedule Telegram reminders.
    Only exact-time events are synced (all-day events are skipped).
    """
    session = SessionLocal()
    try:
        now = datetime.now(tz)
        window_end = now + timedelta(days=GOOGLE_SYNC_DAYS)
        events = list_upcoming_events(time_min=now, time_max=window_end, max_results=100)

        for event in events:
            event_id = event.get("id")
            if not event_id:
                continue

            start_info = event.get("start") or {}
            start_dt_raw = start_info.get("dateTime")
            if not start_dt_raw:
                continue

            try:
                start_at = datetime.fromisoformat(start_dt_raw)
                start_at = ensure_tzaware(start_at)
            except Exception:
                continue

            if start_at <= now:
                continue

            existing = (
                session.query(Task)
                .filter(Task.calendar_event_id == event_id)
                .first()
            )
            if existing:
                continue

            task_text = (event.get("summary") or "Calendar event").strip()
            reminder_datetimes = [
                start_at - timedelta(minutes=5),
                start_at,
            ]
            times_csv = ",".join([rd.strftime("%H:%M") for rd in reminder_datetimes])

            db_task = Task(
                raw_text=task_text,
                task=task_text,
                date=start_at.date(),
                times_csv=times_csv,
                start_at=start_at,
                calendar_event_id=event_id,
                has_exact_time=True,
                is_range=False,
                completed=False,
            )
            session.add(db_task)
            session.flush()

            for run_at in reminder_datetimes:
                run_at = ensure_tzaware(run_at)
                reminder = Reminder(task_id=db_task.id, run_at=run_at, sent=False)
                session.add(reminder)
                session.flush()
                schedule_reminder_job(reminder.id, run_at)

        session.commit()
    except Exception:
        log_exception("sync_google_calendar_events failed")
    finally:
        session.close()


@app.on_event("startup")
def startup():
    try:
        scheduler.start()
        schedule_pending_reminders_from_db()
        scheduler.add_job(
            sync_google_calendar_events,
            trigger="interval",
            minutes=GOOGLE_SYNC_INTERVAL_MINUTES,
            id="google_calendar_sync",
            replace_existing=True,
        )
        sync_google_calendar_events()
    except Exception:
        log_exception("startup failed")


# ----------------------------
# Main endpoint
# ----------------------------
@app.post("/add_task")
async def add_task(request: Request):
    try:
        data = await request.json()
        raw_text = (data.get("text") or "").strip()
        logger.info("[DEBUG] add_task received: %s", raw_text)
        if not raw_text:
            return {"ok": False, "error": "empty_text"}
        print(f"[DEBUG] add_task received: {raw_text}")
        
        now_dt = datetime.now(tz)
        db_context = fetch_recent_tasks_context(limit=25)
        now_iso = now_dt.isoformat()

        dates = []
        task_text = None
        is_range = False
        custom_times = None
        start_at = None
        has_exact_time_task = False

        # 1) Ollama first (if enabled and up)
        if USE_OLLAMA == "1" and ollama_is_up():
            ollama_result = parse_with_ollama(raw_text, now_iso=now_iso, db_context_json=db_context)
        else:
            ollama_result = None

        if ollama_result:
            task_text = (ollama_result.get("task") or raw_text).strip()
            custom_times = ollama_result.get("times")

            start_at_raw = ollama_result.get("start_at")
            if start_at_raw:
                try:
                    start_at = datetime.fromisoformat(start_at_raw)
                    start_at = ensure_tzaware(start_at)
                    has_exact_time_task = True
                except Exception:
                    start_at = None

            for d in ollama_result.get("dates", []):
                try:
                    dates.append(datetime.fromisoformat(d).date())
                except Exception:
                    pass

            if not start_at and custom_times and len(dates) == 1:
                try:
                    hh, mm = custom_times[0].split(":")
                    start_at = tz.localize(datetime.combine(dates[0], time(int(hh), int(mm))))
                    has_exact_time_task = True
                except Exception:
                    start_at = None

            if has_exact_time_task and start_at:
                dates = [start_at.date()]
                is_range = False

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
                parsed_dt, is_range_dp = extract_datetime_from_text(raw_text)
                parsed_dt = ensure_tzaware(parsed_dt)
                dates = [parsed_dt.date()]
                is_range = is_range or is_range_dp
                if has_explicit_time(parsed_dt):
                    start_at = parsed_dt
                    has_exact_time_task = True
                if not task_text:
                    cleaned = raw_text
                    for w in ["today", "tomorrow", "every day this week"]:
                        cleaned = cleaned.replace(w, "")
                    task_text = cleaned.strip().capitalize() or raw_text

        # 3) times
        reminder_list = reminder_times()
        if custom_times and not has_exact_time_task:
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
            if has_exact_time_task and start_at:
                existing = (
                    session.query(Task)
                    .filter(Task.completed == False)
                    .filter(Task.task == task_text)
                    .filter(Task.has_exact_time == True)
                    .filter(Task.start_at == start_at)
                    .all()
                )
            else:
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
        calendar_event_id = None
        session = SessionLocal()
        try:
            if has_exact_time_task and start_at:
                reminder_datetimes = [
                    start_at - timedelta(minutes=5),
                    start_at,
                ]
                times_csv = ",".join([rd.strftime("%H:%M") for rd in reminder_datetimes])
            else:
                reminder_datetimes = []
                times_csv = ",".join([t.strftime("%H:%M") for t in reminder_list])

            for d in dates:
                if has_exact_time_task and start_at:
                    try:
                        calendar_event_id = create_calendar_event(task_text, start_at, TIMEZONE)
                    except Exception:
                        calendar_event_id = None

                db_task = Task(
                    raw_text=raw_text,
                    task=task_text,
                    date=d,
                    times_csv=times_csv,
                    start_at=start_at if has_exact_time_task else None,
                    calendar_event_id=calendar_event_id if has_exact_time_task else None,
                    has_exact_time=has_exact_time_task,
                    is_range=is_range,
                    completed=False,
                )
                session.add(db_task)
                session.flush()

                if has_exact_time_task and start_at:
                    for run_at in reminder_datetimes:
                        run_at = ensure_tzaware(run_at)
                        reminder = Reminder(task_id=db_task.id, run_at=run_at, sent=False)
                        session.add(reminder)
                        session.flush()

                        schedule_reminder_job(reminder.id, run_at)
                        created_reminders += 1
                else:
                    for t in reminder_list:
                        run_at = tz.localize(datetime.combine(d, t))
                        reminder = Reminder(task_id=db_task.id, run_at=run_at, sent=False)
                        session.add(reminder)
                        session.flush()

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
            "has_exact_time": has_exact_time_task,
            "calendar_event_id": calendar_event_id,
        }


    except Exception as exc:
        log_exception("add_task failed")
        return {"ok": False, "error": "add_task_failed", "detail": str(exc)}
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
                "start_at": t.start_at.isoformat() if t.start_at else None,
                "has_exact_time": t.has_exact_time,
                "calendar_event_id": t.calendar_event_id,
                "is_range": t.is_range,
                "completed": t.completed,
            }
            for t in tasks
        ]
    except Exception:
        log_exception("list_tasks failed")
        return {"ok": False, "error": "list_tasks_failed"}
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
    except Exception:
        log_exception("mark_done failed")
        return {"ok": False, "error": "mark_done_failed"}
    finally:
        session.close()


# ----------------------------
# Telegram test
# ----------------------------
@app.get("/telegram_test")
def telegram_test():
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return {"ok": False, "error": "missing_telegram_env"}
        return telegram_send_message("Test message from your bot ✅")
    except Exception:
        log_exception("telegram_test failed")
        return {"ok": False, "error": "telegram_test_failed"}


# if __name__ == "__main__":
#     # import uvicorn
#     #
#     # uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
#
#     # payload = {"text": "Remind me to go for dance at 7 AM in the morning tomorrow"}
#     # resp = requests.post(url, json=payload)
#     # data = resp.json()
#     #
#     # a = {"text":"Remind me to go for dance at 7 AM in the morning tomorrow"}
#     # b = add_task(json.loads(json.dumps(a)))
#     # print(b)
#
#     class FakeRequest:
#         def __init__(self, payload):
#             self._payload = payload
#
#         def json(self):
#             return self._payload
#
#
#     a = {"text": "Remind me to go for dance at 7 AM tomorrow morning"}
#     b = add_task(FakeRequest(a))
#     print(b)

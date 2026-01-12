Voice Task Reminder Bot (Telegram + NLP)

A personal voice-driven task manager that listens to natural language, understands intent and dates, and aggressively reminds you via Telegram. Speak a task like:

“On weekend remind me to wash clothes”

“Tomorrow remind me to apply to 10 jobs”

“Next weekend remind me to clean my room”

The system parses intent, resolves dates intelligently, deduplicates tasks, and schedules multiple reminders automatically.

Why This Exists

Most reminder apps fail at one of these:

Natural language understanding

Flexible scheduling (weekend, ranges, vague phrases)

Persistent, annoying reminders across devices

This project solves all three using:

Voice input (via iOS Shortcuts or any HTTP client)

Smart NLP with fallback safety

Telegram as the universal notification layer

Key Features

Voice-first input – accepts free-form spoken tasks over /add_task.

Smart date understanding – today, tomorrow, weekdays, weekend / next weekend, relative phrases (“in 2 days”), and multi-day ranges.

Ollama-powered NLP (optional) – uses a local LLM when available, but falls back to rules/dateparser when AI is unavailable.

Exact-time tasks – if a task includes a specific time, a Google Calendar event is created and reminders are sent 5 minutes before and at the exact time.

Database-aware intelligence – recent tasks are passed to the LLM to prevent duplicates; DB-level dedupe also exists.

Aggressive reminders – multiple reminders per day by default (configured in reminder_times()); custom times supported via NLP.

Exact scheduling, no polling – reminders are scheduled as one-time APScheduler jobs at the exact datetime. No “check every N seconds” loop.

Telegram delivery – works on iPhone, desktop, and web without building a UI.

Fully containerized – runs anywhere Docker is available, safe to push to GitHub.

Architecture Overview
Voice (iOS Shortcut / HTTP client)
        ↓
FastAPI /add_task
        ↓
[ Ollama (if available) ]
        ↓
Fallback NLP (dateparser + rules)
        ↓
SQLite (task persistence + dedupe)
        ↓
APScheduler (exact one-time jobs)
        ↓
Telegram Bot Messages

Tech Stack

Backend: Python, FastAPI

NLP: Ollama (local LLM, optional) + dateparser fallback

Database: SQLite via SQLAlchemy

Scheduling: APScheduler (date-trigger jobs)

Notifications: Telegram Bot API

Deployment: Docker

Voice Input: iOS Shortcuts or any HTTP client

Example Commands

These work out of the box:

“Tomorrow remind me to wash hands”

“On weekend remind me to wash clothes”

“Next weekend clean my room”

“Every day this week apply to jobs”

If Ollama is running, complex phrases are parsed more accurately. If not, the fallback still works reliably.

Setup
1) Create a Telegram bot + get chat id

In Telegram, talk to @BotFather

Run /newbot and save your bot token

Open your bot chat and hit Start

Get your chat id (easy method):

Use Telegram bot @userinfobot and copy your id

Environment Variables

Create a .env locally (never commit it):

TIMEZONE=America/Chicago

TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789

USE_OLLAMA=0
OLLAMA_MODEL=mistral:latest
OLLAMA_BASE_URL=http://localhost:11434

GOOGLE_CALENDAR_ID=primary
GOOGLE_TOKEN_PATH=token.json
GOOGLE_CREDS_PATH=credentials.json


An example template lives in .env.example.

Running Locally (without Ollama)
docker build -t voice-task-bot .
docker run -p 8000:8000 \
  -e TIMEZONE="America/Chicago" \
  -e TELEGRAM_BOT_TOKEN="123456:ABC..." \
  -e TELEGRAM_CHAT_ID="123456789" \
  -e USE_OLLAMA="0" \
  voice-task-bot

Running Locally (with Ollama)

Start Ollama:

ollama serve


Then run Docker with host access:

docker run -p 8000:8000 \
  -e TIMEZONE="America/Chicago" \
  -e TELEGRAM_BOT_TOKEN="123456:ABC..." \
  -e TELEGRAM_CHAT_ID="123456789" \
  -e USE_OLLAMA="1" \
  -e OLLAMA_BASE_URL="http://host.docker.internal:11434" \
  -e OLLAMA_MODEL="mistral:latest" \
  voice-task-bot

API Endpoints

POST /add_task
Body: {"text": "On weekend remind me to wash clothes"}

GET /tasks
Lists tasks.

POST /tasks/{task_id}/done
Marks a task completed.

GET /telegram_test
Sends a test Telegram message (useful for setup validation).

Google Calendar Setup (exact-time tasks only)

1) Create a Google Cloud project and enable the Google Calendar API
2) Create OAuth credentials and download credentials.json
3) Place credentials.json in the project root (or set GOOGLE_CREDS_PATH)
4) The first exact-time task will prompt an OAuth flow and store token.json

How Many Notifications Will I Get?

By default, the bot sends one reminder per time in reminder_times().

If reminder_times() returns 6 times, then:

“Remind me tomorrow to wash clothes” → 6 Telegram messages tomorrow (one at each configured time)

Data Persistence Notes

SQLite keeps things simple; tasks dedupe via (task, date, completed=False).

Reminders are stored in a reminders table and also scheduled as APScheduler one-time jobs.

On restart, the scheduler rehydrates jobs from the DB so future reminders still fire.

Safety & Privacy

Secrets are never committed; use environment variables.

No cloud AI APIs are used.

Ollama runs entirely locally when enabled.

Future Improvements

Telegram “DONE” reply auto-completes tasks.

Snooze (“remind me again in 30 minutes”).

Recurring schedules (“every weekday”).

Web dashboard for task review.

Postgres backend for long-term persistence.

Remote hosting option (always-on) instead of relying on a MacBook.

Who This Is For

People who want reminders that won’t let them forget.

Anyone building practical NLP tooling with safe fallbacks.

A resume-ready backend + scheduling + persistence project.

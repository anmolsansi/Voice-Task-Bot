## Voice Task Reminder Bot (Slack + NLP)

A personal voice-driven task manager that listens to natural language, understands intent and dates, and aggressively reminds you via Slack. Speak a task like:

- “On weekend remind me to wash clothes”
- “Tomorrow remind me to apply to 10 jobs”
- “Next weekend remind me to clean my room”

The system parses intent, resolves dates intelligently, deduplicates tasks, and schedules multiple reminders automatically.

### Why This Exists

Most reminder apps fail at one of these:

- Natural language understanding
- Flexible scheduling (weekend, ranges, vague phrases)
- Persistent, annoying reminders across devices

This project solves all three using:

- Voice input (via iOS Shortcuts or any HTTP client)
- Smart NLP with fallback safety
- Slack as the universal notification layer

### Key Features

- **Voice-first input** – accepts free-form spoken tasks over `/add_task`.
- **Smart date understanding** – today, tomorrow, weekdays, weekend / next weekend, relative phrases (“in 2 days”), and multi-day ranges.
- **Ollama-powered NLP (optional)** – uses a local LLM when available, but gracefully falls back to rules/dateparser without AI.
- **Database-aware intelligence** – recent tasks are passed to the LLM to avoid duplicates automatically.
- **Aggressive reminders** – 5 reminders per day by default; custom times supported via NLP.
- **Slack-native delivery** – mobile, desktop, and web support without a custom frontend.
- **Fully containerized** – runs anywhere Docker is available, safe to push to GitHub.

### Architecture Overview

```
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
Slack Scheduled Messages
```

### Tech Stack

- **Backend:** Python, FastAPI
- **NLP:** Ollama (local LLM, optional) + dateparser fallback
- **Database:** SQLite via SQLAlchemy
- **Notifications:** Slack API
- **Deployment:** Docker
- **Voice Input:** iOS Shortcuts or any HTTP client

### Example Commands

These all work out of the box:

- “Tomorrow remind me to wash hands”
- “On weekend remind me to wash clothes”
- “Next weekend clean my room”
- “Every day this week apply to jobs”

If Ollama is running, complex phrases are parsed more accurately. If not, the fallback still works reliably.

### Running Locally (without Ollama)

```bash
docker build -t voice-task-bot .
docker run -p 8000:8000 \
  -e SLACK_BOT_TOKEN="xoxb-..." \
  -e SLACK_USER_ID="U..." \
  -e TIMEZONE="America/Chicago" \
  -e USE_OLLAMA="0" \
  voice-task-bot
```

### Running Locally (with Ollama)

Start Ollama:

```bash
ollama serve
```

Then run Docker with host access:

```bash
docker run -p 8000:8000 \
  -e SLACK_BOT_TOKEN="xoxb-..." \
  -e SLACK_USER_ID="U..." \
  -e TIMEZONE="America/Chicago" \
  -e USE_OLLAMA="1" \
  -e OLLAMA_BASE_URL="http://host.docker.internal:11434" \
  -e OLLAMA_MODEL="mistral:latest" \
  voice-task-bot
```

### Environment Variables

Create a `.env` locally (never commit it):

```
SLACK_BOT_TOKEN=your_slack_bot_token
SLACK_USER_ID=your_slack_user_id
TIMEZONE=America/Chicago
USE_OLLAMA=0
OLLAMA_MODEL=mistral:latest
OLLAMA_BASE_URL=http://localhost:11434
```

An example template lives in `.env.example`.

### API Endpoints

- **POST `/add_task`** – body: `{"text": "On weekend remind me to wash clothes"}`
- **GET `/tasks`** – list pending tasks.
- **POST `/tasks/{task_id}/done`** – mark a task completed.

### Data Persistence Notes

- SQLite keeps things simple; tasks dedupe via `(task, date, completed=False)`.
- In containerized deployments, SQLite may reset on rebuilds; migrate to Postgres for long-term use (SQLAlchemy models are ready).

### Safety & Privacy

- Secrets are never committed; use environment variables.
- No cloud AI APIs are used.
- Ollama runs entirely locally when enabled.

### Future Improvements

- Slack “DONE” replies auto-complete tasks.
- Recurring schedules (“every weekday”).
- Web dashboard for task review.
- Managed Postgres backend.
- Hosted Ollama support.

### Who This Is For

- Engineers who live in Slack.
- People who want reminders that won’t let them forget.
- Anyone experimenting with practical AI + fallback systems.
- A resume-ready backend + NLP project.

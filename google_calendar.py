import os
from datetime import timedelta

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def get_calendar_service():
    """
    Uses OAuth token stored locally.
    For personal use, this is the simplest approach.
    """
    try:
        creds = None

        token_path = os.getenv("GOOGLE_TOKEN_PATH", "token.json")
        creds_path = os.getenv("GOOGLE_CREDS_PATH", "credentials.json")

        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(token_path, "w") as f:
                f.write(creds.to_json())

        return build("calendar", "v3", credentials=creds)
    except Exception as exc:
        raise RuntimeError(f"get_calendar_service failed: {exc}") from exc


def create_calendar_event(summary: str, start_at, timezone: str):
    """
    start_at must be timezone-aware datetime
    """
    try:
        service = get_calendar_service()

        end_at = start_at + timedelta(minutes=30)

        event = {
            "summary": summary,
            "start": {"dateTime": start_at.isoformat(), "timeZone": timezone},
            "end": {"dateTime": end_at.isoformat(), "timeZone": timezone},
        }

        calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
        created = service.events().insert(calendarId=calendar_id, body=event).execute()
        return created["id"]
    except Exception as exc:
        raise RuntimeError(f"create_calendar_event failed: {exc}") from exc

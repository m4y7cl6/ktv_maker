"""
YouTube 上傳模組
OAuth 2.0 授權 + 影片上傳
"""

import json
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES        = ["https://www.googleapis.com/auth/youtube.upload"]
CREDS_DIR     = Path("/app/credentials")
TOKEN_FILE    = CREDS_DIR / "youtube_token.json"
SECRETS_FILE  = CREDS_DIR / "client_secrets.json"
REDIRECT_URI  = "http://localhost:8000/auth/callback"

_pending_flow: Flow | None = None


def has_secrets() -> bool:
    return SECRETS_FILE.exists()


def is_authenticated() -> bool:
    if not TOKEN_FILE.exists():
        return False
    try:
        creds = _load_creds()
        if creds is None:
            return False
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_creds(creds)
        return creds.valid
    except Exception:
        return False


def get_auth_url() -> str:
    global _pending_flow
    _pending_flow = Flow.from_client_secrets_file(
        str(SECRETS_FILE), scopes=SCOPES, redirect_uri=REDIRECT_URI
    )
    url, _ = _pending_flow.authorization_url(prompt="consent", access_type="offline")
    return url


def exchange_code(code: str) -> None:
    global _pending_flow
    flow = _pending_flow or Flow.from_client_secrets_file(
        str(SECRETS_FILE), scopes=SCOPES, redirect_uri=REDIRECT_URI
    )
    flow.fetch_token(code=code)
    _save_creds(flow.credentials)
    _pending_flow = None


def upload_video(video_path: Path, title: str, privacy: str = "private") -> str:
    """上傳影片，回傳 YouTube video ID。"""
    creds = _load_creds()
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_creds(creds)

    youtube = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": title,
            "description": "由 KTV Maker AI 自動產製",
            "tags": ["KTV", "karaoke", "伴奏", "去人聲"],
            "categoryId": "10",  # Music
        },
        "status": {"privacyStatus": privacy},
    }
    media = MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True)
    response = (
        youtube.videos()
        .insert(part="snippet,status", body=body, media_body=media)
        .execute()
    )
    return response["id"]


def _load_creds() -> Credentials | None:
    if not TOKEN_FILE.exists():
        return None
    data = json.loads(TOKEN_FILE.read_text())
    return Credentials.from_authorized_user_info(data, SCOPES)


def _save_creds(creds: Credentials) -> None:
    CREDS_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json())

from __future__ import annotations

import html
import json
import os
import re
import time
from base64 import urlsafe_b64decode
from dataclasses import asdict, dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


GMAIL_API_ROOT = "https://gmail.googleapis.com/gmail/v1"
GOOGLE_AUTH_ROOT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


@dataclass(frozen=True)
class GmailConfig:
    client_id: str
    client_secret: str
    scopes: str
    redirect_uri: str = "http://127.0.0.1:8080/auth/gmail/callback"

    @classmethod
    def from_env(cls) -> "GmailConfig":
        return cls(
            client_id=os.getenv("GMAIL_CLIENT_ID", "").strip(),
            client_secret=os.getenv("GMAIL_CLIENT_SECRET", "").strip(),
            scopes=os.getenv(
                "GMAIL_SCOPES",
                "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send",
            ).strip(),
            redirect_uri=os.getenv(
                "GMAIL_REDIRECT_URI",
                "http://127.0.0.1:8080/auth/gmail/callback",
            ).strip(),
        )

    @classmethod
    def from_settings(cls, settings: dict[str, Any] | None) -> "GmailConfig":
        env_config = cls.from_env()
        if not settings:
            return env_config
        return cls(
            client_id=str(settings.get("client_id") or env_config.client_id).strip(),
            client_secret=str(settings.get("client_secret") or env_config.client_secret).strip(),
            scopes=str(settings.get("scopes") or env_config.scopes).strip(),
            redirect_uri=str(settings.get("redirect_uri") or env_config.redirect_uri).strip(),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)


@dataclass(frozen=True)
class GmailMessage:
    id: str
    subject: str
    sender: str
    received_at: str | None
    body_preview: str
    body: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GmailFileAttachment:
    id: str
    name: str
    content_type: str
    content: bytes


class GmailAuthError(Exception):
    pass


class GmailClient:
    def __init__(self, config: GmailConfig | None = None) -> None:
        self.config = config or GmailConfig.from_env()

    def configured_status(self, has_token: bool) -> dict[str, Any]:
        return {
            "configured": self.config.is_configured,
            "connected": has_token,
            "scopes": self.config.scopes,
            "redirect_uri": self.config.redirect_uri,
        }

    def authorization_url(self, state: str) -> str:
        self._require_config()
        query = urlencode(
            {
                "client_id": self.config.client_id,
                "redirect_uri": self.config.redirect_uri,
                "response_type": "code",
                "scope": self.config.scopes,
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
            }
        )
        return f"{GOOGLE_AUTH_ROOT}?{query}"

    def exchange_authorization_code(self, code: str) -> dict[str, Any]:
        self._require_config()
        return self._with_expiry(
            self._post_form(
                GOOGLE_TOKEN_URL,
                {
                    "grant_type": "authorization_code",
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "code": code,
                    "redirect_uri": self.config.redirect_uri,
                },
            )
        )

    def refresh_token(self, token: dict[str, Any]) -> dict[str, Any]:
        self._require_config()
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            raise GmailAuthError("missing_refresh_token")
        refreshed = self._post_form(
            GOOGLE_TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "refresh_token": refresh_token,
            },
        )
        if "refresh_token" not in refreshed:
            refreshed["refresh_token"] = refresh_token
        return self._with_expiry(refreshed)

    def list_inbox_messages(
        self,
        token: dict[str, Any],
        top: int = 100,
        received_since: str | None = None,
    ) -> list[GmailMessage]:
        max_results = max(1, min(top, 500))
        page_size = min(max_results, 100)
        query_parts = ["in:inbox"]
        if received_since:
            query_parts.append(f"after:{_gmail_after_date(received_since)}")
        query = urlencode(
            {
                "maxResults": str(page_size),
                "q": " ".join(query_parts),
            }
        )
        path = f"/users/me/messages?{query}"
        messages: list[GmailMessage] = []
        while path and len(messages) < max_results:
            response = self._get_gmail(path, token)
            for item in response.get("messages", []):
                if len(messages) >= max_results:
                    break
                messages.append(self.get_message(token, item["id"]))
            next_token = response.get("nextPageToken")
            path = f"/users/me/messages?{query}&pageToken={quote(next_token)}" if next_token else ""
        return messages

    def get_message(self, token: dict[str, Any], message_id: str) -> GmailMessage:
        encoded_id = quote(message_id, safe="")
        response = self._get_gmail(f"/users/me/messages/{encoded_id}?format=full", token)
        return self._parse_message(response)

    def list_file_attachments(
        self,
        token: dict[str, Any],
        message_id: str,
    ) -> list[GmailFileAttachment]:
        message = self._get_gmail(f"/users/me/messages/{quote(message_id, safe='')}?format=full", token)
        parts = _flatten_parts(message.get("payload", {}))
        attachments: list[GmailFileAttachment] = []
        for part in parts:
            filename = part.get("filename") or ""
            body = part.get("body", {}) or {}
            attachment_id = body.get("attachmentId")
            if not filename or not attachment_id:
                continue
            data = self._get_gmail(
                f"/users/me/messages/{quote(message_id, safe='')}/attachments/{quote(attachment_id, safe='')}",
                token,
            ).get("data", "")
            if not data:
                continue
            attachments.append(
                GmailFileAttachment(
                    id=attachment_id,
                    name=filename,
                    content_type=part.get("mimeType", "") or "",
                    content=_decode_base64url(data),
                )
            )
        return attachments

    def send_mail(self, token: dict[str, Any], to_address: str, subject: str, body: str) -> None:
        raw = (
            f"To: {to_address}\r\n"
            f"Subject: {subject}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            f"{body}"
        ).encode("utf-8")
        payload = {"raw": _encode_base64url(raw)}
        self._post_gmail("/users/me/messages/send", token, payload)

    def _parse_message(self, item: dict[str, Any]) -> GmailMessage:
        payload = item.get("payload", {}) or {}
        headers = {
            str(header.get("name", "")).lower(): str(header.get("value", ""))
            for header in payload.get("headers", [])
        }
        body = _extract_body(payload)
        received_at = _millis_to_iso(item.get("internalDate"))
        snippet = item.get("snippet", "") or body[:240]
        return GmailMessage(
            id=item.get("id", ""),
            subject=headers.get("subject") or "(no subject)",
            sender=headers.get("from") or "",
            received_at=received_at,
            body_preview=html.unescape(snippet),
            body=body,
        )

    def _get_gmail(self, path: str, token: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            f"{GMAIL_API_ROOT}{path}",
            headers={
                "Authorization": f"Bearer {token['access_token']}",
                "Accept": "application/json",
            },
            method="GET",
        )
        return self._open_json(request)

    def _post_gmail(self, path: str, token: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            f"{GMAIL_API_ROOT}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token['access_token']}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        return self._open_json(request)

    def _post_form(self, url: str, data: dict[str, str]) -> dict[str, Any]:
        request = Request(
            url,
            data=urlencode(data).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        return self._open_json(request)

    def _open_json(self, request: Request) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except HTTPError as exc:
            payload = exc.read().decode("utf-8")
            try:
                error = json.loads(payload).get("error", {})
                if isinstance(error, dict):
                    raise GmailAuthError(str(error.get("message") or error.get("code")))
                raise GmailAuthError(str(error))
            except json.JSONDecodeError as parse_exc:
                raise GmailAuthError(payload or str(exc)) from parse_exc

    def _with_expiry(self, token: dict[str, Any]) -> dict[str, Any]:
        result = dict(token)
        expires_in = int(result.get("expires_in", 3600))
        result["expires_at"] = time.time() + expires_in
        return result

    def _require_config(self) -> None:
        if not self.config.is_configured:
            raise ValueError("Gmail Client ID, Client Secret, and Redirect URI are required")


def _flatten_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    parts = [payload]
    for part in payload.get("parts", []) or []:
        parts.extend(_flatten_parts(part))
    return parts


def _extract_body(payload: dict[str, Any]) -> str:
    plain = []
    html_parts = []
    for part in _flatten_parts(payload):
        mime_type = part.get("mimeType", "")
        data = (part.get("body", {}) or {}).get("data", "")
        if not data:
            continue
        text = _decode_base64url(data).decode("utf-8", errors="replace")
        if mime_type == "text/plain":
            plain.append(text)
        elif mime_type == "text/html":
            html_parts.append(_html_to_text(text))
    return "\n".join(plain or html_parts)


def _decode_base64url(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return urlsafe_b64decode(padded.encode("ascii"))


def _encode_base64url(value: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _html_to_text(value: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p\s*>", "\n", text)
    text = re.sub(r"(?is)<.*?>", " ", text)
    return html.unescape(re.sub(r"[ \t]+", " ", text)).strip()


def _millis_to_iso(value: Any) -> str | None:
    try:
        return datetime_from_millis(int(value))
    except (TypeError, ValueError):
        return None


def datetime_from_millis(value: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def _gmail_after_date(received_since: str) -> str:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(received_since.replace("Z", "+00:00"))
        return parsed.date().isoformat()
    except ValueError:
        return received_since[:10]

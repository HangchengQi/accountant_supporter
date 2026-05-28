from __future__ import annotations

import html
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GRAPH_ROOT = "https://graph.microsoft.com/v1.0"


@dataclass(frozen=True)
class OutlookConfig:
    client_id: str
    tenant_id: str
    scopes: str

    @classmethod
    def from_env(cls) -> "OutlookConfig":
        return cls(
            client_id=os.getenv("OUTLOOK_CLIENT_ID", "").strip(),
            tenant_id=os.getenv("OUTLOOK_TENANT_ID", "common").strip() or "common",
            scopes=os.getenv(
                "OUTLOOK_SCOPES",
                "offline_access User.Read Mail.Read",
            ).strip(),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id)


@dataclass(frozen=True)
class DeviceCodeSession:
    device_code: str
    user_code: str
    verification_uri: str
    expires_at: float
    interval: int
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_code": self.user_code,
            "verification_uri": self.verification_uri,
            "expires_in": max(0, int(self.expires_at - time.time())),
            "interval": self.interval,
            "message": self.message,
        }


@dataclass(frozen=True)
class OutlookMessage:
    id: str
    subject: str
    sender: str
    received_at: str | None
    body_preview: str
    body: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OutlookAuthError(Exception):
    pass


class OutlookGraphClient:
    def __init__(self, config: OutlookConfig | None = None) -> None:
        self.config = config or OutlookConfig.from_env()

    def configured_status(self, has_token: bool) -> dict[str, Any]:
        return {
            "configured": self.config.is_configured,
            "connected": has_token,
            "tenant_id": self.config.tenant_id,
            "scopes": self.config.scopes,
        }

    def start_device_code(self) -> DeviceCodeSession:
        self._require_config()
        response = self._post_form(
            self._auth_url("devicecode"),
            {
                "client_id": self.config.client_id,
                "scope": self.config.scopes,
            },
        )
        return DeviceCodeSession(
            device_code=response["device_code"],
            user_code=response["user_code"],
            verification_uri=response.get("verification_uri")
            or response.get("verification_url", ""),
            expires_at=time.time() + int(response.get("expires_in", 900)),
            interval=int(response.get("interval", 5)),
            message=response.get("message", ""),
        )

    def poll_device_code(self, session: DeviceCodeSession) -> dict[str, Any]:
        self._require_config()
        if time.time() >= session.expires_at:
            return {"status": "expired"}
        try:
            token = self._post_form(
                self._auth_url("token"),
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": self.config.client_id,
                    "device_code": session.device_code,
                },
            )
        except OutlookAuthError as exc:
            code = str(exc)
            if code in {"authorization_pending", "slow_down"}:
                return {"status": code}
            raise
        return {"status": "connected", "token": self._with_expiry(token)}

    def refresh_token(self, token: dict[str, Any]) -> dict[str, Any]:
        self._require_config()
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            raise OutlookAuthError("missing_refresh_token")
        refreshed = self._post_form(
            self._auth_url("token"),
            {
                "grant_type": "refresh_token",
                "client_id": self.config.client_id,
                "refresh_token": refresh_token,
                "scope": self.config.scopes,
            },
        )
        if "refresh_token" not in refreshed:
            refreshed["refresh_token"] = refresh_token
        return self._with_expiry(refreshed)

    def list_inbox_messages(self, token: dict[str, Any], top: int = 10) -> list[OutlookMessage]:
        safe_top = max(1, min(top, 25))
        query = urlencode(
            {
                "$top": str(safe_top),
                "$select": "id,subject,from,receivedDateTime,bodyPreview,body",
                "$orderby": "receivedDateTime desc",
            }
        )
        response = self._get_graph(f"/me/mailFolders/Inbox/messages?{query}", token)
        return [self._parse_message(item) for item in response.get("value", [])]

    def _parse_message(self, item: dict[str, Any]) -> OutlookMessage:
        sender = (
            item.get("from", {})
            .get("emailAddress", {})
            .get("name")
            or item.get("from", {}).get("emailAddress", {}).get("address")
            or ""
        )
        body = item.get("body", {}).get("content", "") or ""
        return OutlookMessage(
            id=item.get("id", ""),
            subject=item.get("subject", "") or "(no subject)",
            sender=sender,
            received_at=item.get("receivedDateTime"),
            body_preview=item.get("bodyPreview", "") or "",
            body=self._html_to_text(body),
        )

    def _get_graph(self, path: str, token: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            f"{GRAPH_ROOT}{path}",
            headers={
                "Authorization": f"Bearer {token['access_token']}",
                "Accept": "application/json",
            },
            method="GET",
        )
        return self._open_json(request)

    def _post_form(self, url: str, data: dict[str, str]) -> dict[str, Any]:
        body = urlencode(data).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        return self._open_json(request)

    def _open_json(self, request: Request) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            payload = exc.read().decode("utf-8")
            try:
                error = json.loads(payload).get("error", {})
                if isinstance(error, dict):
                    raise OutlookAuthError(str(error.get("code") or error.get("message")))
                raise OutlookAuthError(str(error))
            except json.JSONDecodeError as parse_exc:
                raise OutlookAuthError(payload or str(exc)) from parse_exc

    def _auth_url(self, endpoint: str) -> str:
        return (
            f"https://login.microsoftonline.com/{self.config.tenant_id}"
            f"/oauth2/v2.0/{endpoint}"
        )

    def _require_config(self) -> None:
        if not self.config.is_configured:
            raise ValueError("OUTLOOK_CLIENT_ID is required")

    def _with_expiry(self, token: dict[str, Any]) -> dict[str, Any]:
        result = dict(token)
        result["expires_at"] = time.time() + int(result.get("expires_in", 3600))
        return result

    def _html_to_text(self, value: str) -> str:
        without_tags = re.sub(r"<(br|p|div|li)\b[^>]*>", "\n", value, flags=re.I)
        without_tags = re.sub(r"<[^>]+>", " ", without_tags)
        return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()

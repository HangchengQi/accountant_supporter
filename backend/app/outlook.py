from __future__ import annotations

import html
import json
import os
import re
import time
from base64 import b64decode
from dataclasses import asdict, dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


GRAPH_ROOT = "https://graph.microsoft.com/v1.0"


@dataclass(frozen=True)
class OutlookConfig:
    client_id: str
    tenant_id: str
    scopes: str
    client_secret: str = ""
    redirect_uri: str = "http://127.0.0.1:8080/auth/outlook/callback"

    @classmethod
    def from_env(cls) -> "OutlookConfig":
        return cls(
            client_id=os.getenv("OUTLOOK_CLIENT_ID", "").strip(),
            tenant_id=os.getenv("OUTLOOK_TENANT_ID", "common").strip() or "common",
            scopes=os.getenv(
                "OUTLOOK_SCOPES",
                "offline_access User.Read Mail.Read Mail.Send",
            ).strip(),
            client_secret=os.getenv("OUTLOOK_CLIENT_SECRET", "").strip(),
            redirect_uri=os.getenv(
                "OUTLOOK_REDIRECT_URI",
                "http://127.0.0.1:8080/auth/outlook/callback",
            ).strip(),
        )

    @classmethod
    def from_settings(cls, settings: dict[str, Any] | None) -> "OutlookConfig":
        env_config = cls.from_env()
        if not settings:
            return env_config
        return cls(
            client_id=str(settings.get("client_id") or env_config.client_id).strip(),
            tenant_id=str(settings.get("tenant_id") or env_config.tenant_id or "common").strip(),
            scopes=str(settings.get("scopes") or env_config.scopes).strip(),
            client_secret=str(settings.get("client_secret") or env_config.client_secret).strip(),
            redirect_uri=str(settings.get("redirect_uri") or env_config.redirect_uri).strip(),
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


@dataclass(frozen=True)
class OutlookFileAttachment:
    id: str
    name: str
    content_type: str
    content: bytes


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
            "redirect_uri": self.config.redirect_uri,
        }

    def authorization_url(self, state: str) -> str:
        self._require_config()
        query = urlencode(
            {
                "client_id": self.config.client_id,
                "response_type": "code",
                "redirect_uri": self.config.redirect_uri,
                "response_mode": "query",
                "scope": self.config.scopes,
                "state": state,
                "prompt": "select_account",
            }
        )
        return f"{self._auth_url('authorize')}?{query}"

    def exchange_authorization_code(self, code: str) -> dict[str, Any]:
        self._require_config()
        payload = {
            "grant_type": "authorization_code",
            "client_id": self.config.client_id,
            "code": code,
            "redirect_uri": self.config.redirect_uri,
            "scope": self.config.scopes,
        }
        if self.config.client_secret:
            payload["client_secret"] = self.config.client_secret
        return self._with_expiry(self._post_form(self._auth_url("token"), payload))

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
            self._with_optional_secret(
                {
                    "grant_type": "refresh_token",
                    "client_id": self.config.client_id,
                    "refresh_token": refresh_token,
                    "scope": self.config.scopes,
                }
            ),
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

    def list_file_attachments(
        self,
        token: dict[str, Any],
        message_id: str,
    ) -> list[OutlookFileAttachment]:
        encoded_id = quote(message_id, safe="")
        query = urlencode(
            {
                "$select": "id,name,contentType,contentBytes",
            }
        )
        response = self._get_graph(f"/me/messages/{encoded_id}/attachments?{query}", token)
        attachments = []
        for item in response.get("value", []):
            if item.get("@odata.type") != "#microsoft.graph.fileAttachment":
                continue
            content = item.get("contentBytes")
            if not content:
                continue
            attachments.append(
                OutlookFileAttachment(
                    id=item.get("id", ""),
                    name=item.get("name", "attachment.bin") or "attachment.bin",
                    content_type=item.get("contentType", "") or "",
                    content=b64decode(content),
                )
            )
        return attachments

    def send_mail(self, token: dict[str, Any], to_address: str, subject: str, body: str) -> None:
        payload = {
            "message": {
                "subject": subject,
                "body": {
                    "contentType": "Text",
                    "content": body,
                },
                "toRecipients": [
                    {
                        "emailAddress": {
                            "address": to_address,
                        }
                    }
                ],
            },
            "saveToSentItems": True,
        }
        self._post_graph("/me/sendMail", token, payload)

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

    def _post_graph(self, path: str, token: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            f"{GRAPH_ROOT}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token['access_token']}",
                "Content-Type": "application/json",
            },
            method="POST",
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

    def _with_optional_secret(self, data: dict[str, str]) -> dict[str, str]:
        result = dict(data)
        if self.config.client_secret:
            result["client_secret"] = self.config.client_secret
        return result

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

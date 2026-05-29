from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .schemas import EmailSampleIn, ExtractedFields
from .workflow import Workflow


ZOHO_ACCOUNTS_ROOT = "https://accounts.zoho.com"


@dataclass(frozen=True)
class ZohoConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: str
    accounts_root: str = ZOHO_ACCOUNTS_ROOT

    @classmethod
    def from_env(cls) -> "ZohoConfig":
        return cls(
            client_id=os.getenv("ZOHO_CLIENT_ID", "").strip(),
            client_secret=os.getenv("ZOHO_CLIENT_SECRET", "").strip(),
            redirect_uri=os.getenv(
                "ZOHO_REDIRECT_URI",
                "http://127.0.0.1:8080/auth/zoho/callback",
            ).strip(),
            scopes=os.getenv("ZOHO_SCOPES", "ZohoBooks.fullaccess.all").strip(),
            accounts_root=os.getenv("ZOHO_ACCOUNTS_ROOT", ZOHO_ACCOUNTS_ROOT).strip()
            or ZOHO_ACCOUNTS_ROOT,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)


class ZohoAuthError(Exception):
    pass


class ZohoOAuthClient:
    def __init__(self, config: ZohoConfig | None = None) -> None:
        self.config = config or ZohoConfig.from_env()

    def configured_status(self, has_token: bool) -> dict[str, Any]:
        return {
            "configured": self.config.is_configured,
            "connected": has_token,
            "scopes": self.config.scopes,
            "redirect_uri": self.config.redirect_uri,
            "accounts_root": self.config.accounts_root,
        }

    def authorization_url(self, state: str) -> str:
        self._require_config()
        query = urlencode(
            {
                "scope": self.config.scopes,
                "client_id": self.config.client_id,
                "state": state,
                "response_type": "code",
                "redirect_uri": self.config.redirect_uri,
                "access_type": "offline",
                "prompt": "consent",
            }
        )
        return f"{self.config.accounts_root}/oauth/v2/auth?{query}"

    def exchange_authorization_code(self, code: str) -> dict[str, Any]:
        self._require_config()
        token = self._post_form(
            f"{self.config.accounts_root}/oauth/v2/token",
            {
                "code": code,
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "redirect_uri": self.config.redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        return self._with_expiry(token)

    def refresh_token(self, token: dict[str, Any]) -> dict[str, Any]:
        self._require_config()
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            raise ZohoAuthError("missing_refresh_token")
        refreshed = self._post_form(
            f"{self.config.accounts_root}/oauth/v2/token",
            {
                "refresh_token": refresh_token,
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "grant_type": "refresh_token",
            },
        )
        if "refresh_token" not in refreshed:
            refreshed["refresh_token"] = refresh_token
        return self._with_expiry(refreshed)

    def _post_form(self, url: str, data: dict[str, str]) -> dict[str, Any]:
        body = urlencode(data).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            payload = exc.read().decode("utf-8")
            try:
                error = json.loads(payload)
                raise ZohoAuthError(str(error.get("error") or error))
            except json.JSONDecodeError as parse_exc:
                raise ZohoAuthError(payload or str(exc)) from parse_exc

    def _require_config(self) -> None:
        if not self.config.is_configured:
            raise ValueError("Zoho Books OAuth environment variables are required")

    def _with_expiry(self, token: dict[str, Any]) -> dict[str, Any]:
        result = dict(token)
        result["expires_at"] = time.time() + int(result.get("expires_in", 3600))
        return result


class ZohoBooksClient:
    def create_draft_from_email(
        self,
        email: EmailSampleIn,
        extracted: ExtractedFields,
        workflow: Workflow,
    ) -> tuple[str, dict[str, Any]]:
        raise NotImplementedError


class DryRunZohoBooksClient(ZohoBooksClient):
    def create_draft_from_email(
        self,
        email: EmailSampleIn,
        extracted: ExtractedFields,
        workflow: Workflow,
    ) -> tuple[str, dict[str, Any]]:
        payload = {
            "target_object": workflow.raw.get("zoho", {}).get("target_object", "bill_draft"),
            "vendor_name": extracted.vendor_name,
            "invoice_number": extracted.invoice_number,
            "invoice_date": extracted.invoice_date,
            "due_date": extracted.due_date,
            "amount": extracted.amount,
            "currency": extracted.currency,
            "source_email": {
                "subject": email.subject,
                "sender": email.sender,
            },
            "requires_review_before_real_upload": extracted.needs_review,
        }
        return "dry_run_created", payload

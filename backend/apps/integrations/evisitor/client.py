from __future__ import annotations

import json
import logging
import ssl
from typing import Any
from urllib.parse import urlencode

import httpx

from apps.integrations.evisitor.config import EvisitorRuntimeConfig
from apps.integrations.evisitor.exceptions import EvisitorApiError, EvisitorConfigError
from apps.integrations.evisitor.messages import resolve_evisitor_error_message

logger = logging.getLogger(__name__)

_PROD_API_MARKER = "eVisitorRhetos_API"
_TEST_API_MARKER = "testApi"


def _evisitor_ssl_context() -> ssl.SSLContext:
    """eVisitor host još koristi legacy DH parametre; OpenSSL 3 inače baca DH_KEY_TOO_SMALL."""
    ctx = ssl.create_default_context()
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    except ssl.SSLError:
        logger.warning("eVisitor SSL: SECLEVEL=1 nije primijenjen", exc_info=True)
    return ctx


class EvisitorClient:
    def __init__(
        self,
        config: EvisitorRuntimeConfig,
        *,
        timeout: float = 60.0,
    ) -> None:
        self._config = config
        self._ensure_config()
        self._session = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            verify=_evisitor_ssl_context(),
        )

    def _ensure_config(self) -> None:
        cfg = self._config
        if not cfg.enabled:
            raise EvisitorConfigError("eVisitor integracija nije uključena.")
        if not cfg.base_url:
            raise EvisitorConfigError("base_url nije postavljen u IntegrationConfig.")
        if not cfg.username or not cfg.password:
            raise EvisitorConfigError("username / password nisu postavljeni u IntegrationConfig.")
        base = cfg.base_url
        env = (cfg.env or "test").lower()
        if env == "test" and _PROD_API_MARKER in base and _TEST_API_MARKER not in base:
            raise EvisitorConfigError(
                "env=test ali base_url izgleda kao produkcija. Koristite testApi URL."
            )
        if env == "prod":
            if _TEST_API_MARKER in base:
                raise EvisitorConfigError(
                    "env=prod ali base_url je testApi. Koristite produkcijski API URL."
                )
            if _PROD_API_MARKER not in base:
                raise EvisitorConfigError(
                    "env=prod ali base_url ne sadrži eVisitorRhetos_API."
                )
        if env == "test" and not cfg.api_key:
            raise EvisitorConfigError("api_key je obavezan na testnoj okolini.")

    @property
    def _auth_url(self) -> str:
        return f"{self._config.base_url}/Resources/AspNetFormsAuth/Authentication/"

    @property
    def _rest_url(self) -> str:
        return f"{self._config.base_url}/Rest/Htz/"

    def login(self) -> bool:
        payload: dict[str, Any] = {
            "UserName": self._config.username,
            "Password": self._config.password,
            "PersistCookie": False,
        }
        if self._config.api_key:
            payload["apikey"] = self._config.api_key
        try:
            response = self._session.post(f"{self._auth_url}Login", json=payload)
        except httpx.TimeoutException as exc:
            raise EvisitorApiError(
                "eVisitor zahtjev je istekao (timeout).",
                user_message="eVisitor zahtjev je istekao (timeout).",
                system_message=str(exc),
            ) from exc
        if response.status_code != 200:
            raise EvisitorApiError(
                f"eVisitor login HTTP {response.status_code}",
                system_message=response.text[:500],
                status_code=response.status_code,
            )
        body = (response.text or "").strip().lower()
        if body != "true":
            raise EvisitorApiError(
                "eVisitor login nije uspio (korisničko ime, lozinka ili apikey).",
                system_message=response.text[:500],
                status_code=response.status_code,
            )
        return True

    def logout(self) -> None:
        try:
            self._session.post(f"{self._auth_url}Logout", json={})
        except httpx.HTTPError:
            logger.debug("eVisitor logout failed", exc_info=True)

    def _extract_error_messages(self, response: httpx.Response) -> tuple[str, str]:
        text = (response.text or "")[:500]
        user_message = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                user_message = str(payload.get("UserMessage") or "")
                system_message = str(payload.get("SystemMessage") or "") or text
                return user_message, system_message
        except json.JSONDecodeError:
            pass
        return user_message, text

    def execute_action(self, action: str, data: dict[str, Any]) -> None:
        url = f"{self._rest_url}{action.strip('/')}/"
        try:
            response = self._session.post(url, json=data)
        except httpx.TimeoutException as exc:
            raise EvisitorApiError(
                "eVisitor zahtjev je istekao (timeout).",
                user_message="eVisitor zahtjev je istekao (timeout).",
                system_message=str(exc),
            ) from exc
        if response.status_code != 200:
            user_message, system_message = self._extract_error_messages(response)
            resolved = resolve_evisitor_error_message(
                user_message=user_message,
                system_message=system_message,
                fallback=f"eVisitor {action} HTTP {response.status_code}",
            )
            raise EvisitorApiError(
                resolved,
                user_message=user_message,
                system_message=system_message,
                status_code=response.status_code,
            )
        text = (response.text or "").strip()
        if not text:
            return
        try:
            payload = response.json()
        except json.JSONDecodeError:
            raise EvisitorApiError(
                f"eVisitor {action} neočekivan odgovor.",
                system_message=text[:500],
                status_code=response.status_code,
            )
        if isinstance(payload, dict) and (
            payload.get("UserMessage") or payload.get("SystemMessage")
        ):
            raise EvisitorApiError(
                payload.get("UserMessage") or "eVisitor validacijska greška.",
                user_message=str(payload.get("UserMessage") or ""),
                system_message=str(payload.get("SystemMessage") or ""),
                status_code=response.status_code,
            )

    def fetch_records(
        self,
        resource: str,
        *,
        psize: int = 50,
        page: int = 1,
        filters: list[dict[str, str]] | None = None,
        sort: str | None = "Code asc",
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"psize": psize, "page": page}
        if sort:
            params["sort"] = sort
        if filters:
            params["filters"] = json.dumps(filters)
        query = urlencode(params)
        url = f"{self._rest_url}{resource.strip('/')}/?{query}"
        response = self._session.get(url)
        if response.status_code != 200:
            raise EvisitorApiError(
                f"eVisitor GET {resource} HTTP {response.status_code}",
                system_message=response.text[:500],
                status_code=response.status_code,
            )
        payload = response.json()
        if isinstance(payload, dict):
            records = payload.get("Records")
            if isinstance(records, list):
                return [r for r in records if isinstance(r, dict)]
        return []

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> EvisitorClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

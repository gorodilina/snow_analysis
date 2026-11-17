"""Shareable, registration-free research sessions stored in Supabase PostgREST.

Server-only access: never send SUPABASE_SECRET_KEY or other credentials to
the browser. Possession of a session URL token grants read/write access.
"""
from __future__ import annotations

import re
import secrets
from datetime import date, datetime
from typing import Any

import requests

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,90}$")
MAX_BYTES = 900_000
MAX_ROUTES = 100
MAX_POINTS = 20_000
MAX_NOTES = 500


class SessionStorageError(RuntimeError):
    """An expected persistence or configuration failure."""


def make_token() -> str:
    """Cryptographically strong 256-bit URL capability."""
    return secrets.token_urlsafe(32)


def valid_token(value: str | None) -> bool:
    return isinstance(value, str) and TOKEN_RE.fullmatch(value) is not None


def normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept only supported fields and cap the size of remote snapshots."""
    if not isinstance(payload, dict):
        raise SessionStorageError("Некорректный формат исследования.")
    routes = payload.get("routes", [])
    points = payload.get("points", [])
    notes = payload.get("notes", [])
    if not all(isinstance(v, list) for v in (routes, points, notes)):
        raise SessionStorageError("Некорректные геоданные или заметки.")
    if len(routes) > MAX_ROUTES or len(points) > MAX_POINTS or len(notes) > MAX_NOTES:
        raise SessionStorageError("Исследование превышает лимит сохранения.")
    cleaned = {
        "schema": 1,
        "name": str(payload.get("name", "Новое исследование"))[:120],
        "routes": routes,
        "points": points,
        "notes": notes,
        "ui": payload.get("ui", {}),
        "selection": payload.get("selection"),  # legacy one-image sessions
        "layers": payload.get("layers", []),  # unlimited descriptors, no raster bytes
        "map_state": payload.get("map_state", {}),
    }
    import json
    body = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(body.encode("utf-8")) > MAX_BYTES:
        raise SessionStorageError("Размер исследования превышает 900 КБ.")
    return cleaned


class SessionStore:
    def __init__(self, url: str, secret_key: str):
        self.url = (url or "").strip().rstrip("/")
        self.secret_key = (secret_key or "").strip()
        if not self.url.startswith("https://") or not self.secret_key:
            raise SessionStorageError("Настройте SUPABASE_URL и SUPABASE_SECRET_KEY.")
        self.endpoint = self.url + "/rest/v1/snow_sessions"

    def _request(self, method: str, **kwargs: Any) -> requests.Response:
        headers = {
            "apikey": self.secret_key,  # sb_secret_* and legacy service_role keys
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": "return=minimal",
        }
        try:
            response = requests.request(
                method, self.endpoint, headers=headers, timeout=(5, 20), **kwargs
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            # Do not accidentally print server request headers or secret keys.
            raise SessionStorageError(
                f"Ошибка хранения исследования (HTTP {getattr(exc.response, 'status_code', 'нет ответа')}). "
                "Проверьте Supabase, таблицу snow_sessions и права."
            ) from None

    def get(self, token: str) -> dict[str, Any] | None:
        if not valid_token(token):
            raise SessionStorageError("Некорректная ссылка на исследование.")
        response = self._request(
            "GET", params={"token": "eq." + token, "select": "payload"}
        )
        rows = response.json()
        if not rows:
            return None
        return normalize_payload(rows[0]["payload"])

    def create(self, payload: dict[str, Any]) -> str:
        token = make_token()
        self._request(
            "POST", json={"token": token, "payload": normalize_payload(payload)}
        )
        return token

    def save(self, token: str, payload: dict[str, Any]) -> None:
        if not valid_token(token):
            raise SessionStorageError("Некорректная ссылка на исследование.")
        self._request(
            "PATCH",
            params={"token": "eq." + token},
            json={
                "payload": normalize_payload(payload),
                "updated_at": datetime.now().astimezone().isoformat(),
            },
        )

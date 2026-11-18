"""Unit tests for URL capability sessions. No Supabase credentials required."""
from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from session_store import (
    SessionStorageError, SessionStore, make_token, normalize_payload, valid_token,
)


def payload():
    return {
        "name": "High Caucasus",
        "routes": [{"name": "Test", "coordinates": [[46, 42], [47, 43]]}],
        "points": [{"name": "Pass", "coordinates": [46.2, 42.2]}],
        "notes": [{"id": "1", "text": "snow on northern slope"}],
        "ui": {"satellite": "Sentinel-2", "mode": "Медианная мозаика"},
        "selection": {"kind": "median", "satellite": "Sentinel-2",
                      "start": "2025-06-01", "length": 5},
        "map_state": {"base": "Рельеф (Esri)", "zoom": 11,
                      "overlays": {"Снимок без маски": {"visible": True, "opacity": .55}}},
    }


def test_tokens_are_strong_url_safe():
    first, second = make_token(), make_token()
    assert first != second
    assert len(first) >= 40
    assert valid_token(first)
    assert not valid_token("too short")
    assert not valid_token("../../etc/passwd")


def test_session_payload_schema_round_trip():
    normalized = normalize_payload(payload())
    assert normalized["schema"] == 1
    assert normalized["routes"][0]["coordinates"][1] == [47, 43]
    assert normalized["notes"][0]["text"] == "snow on northern slope"
    assert normalized["selection"]["kind"] == "median"
    assert normalized["map_state"]["overlays"]["Снимок без маски"]["visible"]


def test_limits_are_enforced():
    with pytest.raises(SessionStorageError):
        normalize_payload({"routes": [], "points": [], "notes": ["x"] * 501})
    with pytest.raises(SessionStorageError):
        normalize_payload({"routes": [], "points": [], "notes": [],
                           "name": "Name", "ui": {"dump": "x" * 950_000}})


def test_secret_key_remains_in_backend_headers_only():
    token = make_token()
    response = Mock()
    response.status_code = 201
    response.raise_for_status.return_value = None
    with patch("session_store.requests.request", return_value=response) as request:
        store = SessionStore("https://example.supabase.co", "sb_secret_TEST")
        assert store.create(payload()) != token
        args, kwargs = request.call_args
        assert args[0] == "POST"
        assert kwargs["headers"]["apikey"] == "sb_secret_TEST"
        assert "Authorization" not in kwargs["headers"]
        assert kwargs["json"]["payload"]["selection"]["kind"] == "median"


def test_get_session_by_token():
    token = make_token()
    response = Mock()
    response.status_code = 200
    response.raise_for_status.return_value = None
    response.json.return_value = [{"payload": payload()}]
    with patch("session_store.requests.request", return_value=response) as request:
        store = SessionStore("https://example.supabase.co", "sb_secret_TEST")
        result = store.get(token)
        assert result["name"] == "High Caucasus"
        assert request.call_args.kwargs["params"]["token"] == "eq." + token


def test_save_updates_only_the_right_token():
    token = make_token()
    response = Mock()
    response.status_code = 204
    response.raise_for_status.return_value = None
    with patch("session_store.requests.request", return_value=response) as request:
        store = SessionStore("https://example.supabase.co", "sb_secret_TEST")
        store.save(token, payload())
        params = request.call_args.kwargs["params"]
        assert params == {"token": "eq." + token}
        assert "updated_at" in request.call_args.kwargs["json"]

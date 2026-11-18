"""Idempotent handling of clipboard events retained by Streamlit components."""
from __future__ import annotations


def record_paste_once(event: dict | None, seen_ids: list[str]) -> bool:
    """Accept each clipboard event once; allow new intentional paste of same image."""
    if not isinstance(event, dict) or event.get("action") != "paste_image":
        return False
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id or len(event_id) > 128:
        return False
    if event_id in seen_ids:
        return False
    seen_ids.append(event_id)
    if len(seen_ids) > 512:
        del seen_ids[:-512]
    return True

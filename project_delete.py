"""Delete a shareable research and all of its private gallery files.

Storage and PostgREST are separate transactions. To prevent dangling private
files, remove Storage files before removing the research database record. If
either service fails, keep the record to allow a safe retry.
"""
from __future__ import annotations

from gallery import GalleryStorage
from session_store import SessionStore, SessionStorageError, valid_token


def delete_project(store: SessionStore, gallery_store: GalleryStorage, token: str) -> int:
    if not valid_token(token):
        raise SessionStorageError("Некорректный токен исследования.")
    if store.get(token) is None:
        raise SessionStorageError("Исследование уже удалено или не найдено.")
    removed_count = gallery_store.remove_project_files(token)
    store.delete(token)
    return removed_count

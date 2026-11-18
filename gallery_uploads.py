"""Background storage uploads for gallery images.

The I/O worker never touches Streamlit session state or the UI. The Streamlit
session polls futures and commits gallery metadata only after upload succeeds.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from functools import lru_cache
from time import perf_counter

from gallery import GalleryStorage


@lru_cache(maxsize=1)
def upload_pool() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=2, thread_name_prefix="snow-gallery-upload")


def submit_gallery_upload(
    storage: GalleryStorage, token: str, item: dict, photo: bytes
) -> Future[tuple[str, float]]:
    safe_item = {"id": item["id"], "format": item["format"]}

    def perform() -> tuple[str, float]:
        start = perf_counter()
        key = storage.put(token, safe_item, photo)
        return key, perf_counter() - start

    return upload_pool().submit(perform)

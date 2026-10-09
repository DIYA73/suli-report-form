# -*- coding: utf-8 -*-
"""
Uploads a file's bytes to Supabase Storage and returns its public URL.

Used instead of saving to local disk when SUPABASE_URL/SUPABASE_SERVICE_KEY
are configured (see config.py) - local disk on free hosting tiers like
Render gets wiped on every restart/redeploy, silently losing every photo
and voice recording citizens submitted before that point.
"""

import logging
import uuid

import httpx

from config import SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_STORAGE_BUCKET

logger = logging.getLogger("storage")

_HTTP_TIMEOUT = 20.0


async def upload_bytes(data: bytes, subdir: str, ext: str, content_type: str) -> str:
    """Uploads `data` to Supabase Storage under `<subdir>/<random-name>.<ext>`
    and returns the public URL. `ext` and `content_type` come from main.py's
    magic-byte sniffing (uploads.py), never from the client's filename, so
    the stored object can't be an .html/.svg that would run as a page.
    Raises on failure (caller should turn that into an HTTP error - we never
    want to silently "succeed" and lose the file)."""
    object_name = f"{subdir}/{uuid.uuid4().hex}.{ext}"

    upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_STORAGE_BUCKET}/{object_name}"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": content_type,
        "x-upsert": "false",
    }

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        res = await client.post(upload_url, headers=headers, content=data)

    if res.status_code not in (200, 201):
        logger.error("Supabase Storage upload failed (%s): %s", res.status_code, res.text)
        raise RuntimeError(f"Supabase Storage upload failed: {res.status_code} {res.text}")

    return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_STORAGE_BUCKET}/{object_name}"

# -*- coding: utf-8 -*-
"""
FastAPI app: serves the Kurdish report form and stores submissions in SQLite
(or Postgres when DATABASE_URL is set).

Run with:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000

Then open http://localhost:8000 in a browser.
"""

import hmac
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import NamedTuple, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import ai
import config
import db
import geocode
import storage
import uploads
from config import (
    CATEGORIES, DATA_DIR, PHOTOS_DIR, AUDIO_DIR, MAX_UPLOAD_BYTES, BASE_DIR,
    MAX_DESCRIPTION_CHARS, MAX_LOCATION_LABEL_CHARS, USE_SUPABASE_STORAGE,
)

logger = logging.getLogger("main")

FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    os.makedirs(AUDIO_DIR, exist_ok=True)
    db.init_db()
    if not config.ADMIN_TOKEN:
        logger.warning(
            "ADMIN_TOKEN is not set: the dashboard data, CSV export, AI re-triage and "
            "media re-attach endpoints are open to anyone who knows the URL. Set the "
            "ADMIN_TOKEN env var to lock them down."
        )
    yield


app = FastAPI(title="Report a Problem API", lifespan=lifespan)

# Wide open for the hackathon demo — the form is meant to be shared widely.
# Tighten this (specific origins) before any real production use.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_admin(request: Request) -> None:
    """Guards the admin-only endpoints. When the ADMIN_TOKEN env var is set,
    callers must send it in an `X-Admin-Token` header (the dashboard asks for
    it once and sends it automatically). When it isn't set, nothing changes:
    the endpoints stay open like before, so deploying this is never a breaking
    change - set the variable to turn the protection on."""
    expected = config.ADMIN_TOKEN
    if not expected:
        return
    supplied = request.headers.get("x-admin-token", "")
    if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(401, "Admin token required")


admin_only = [Depends(require_admin)]


@app.get("/api/config")
def get_config():
    return {"categories": CATEGORIES}


@app.get("/api/health")
def health():
    return {"status": "ok", "reports": db.count_reports()}


class _Blob(NamedTuple):
    data: bytes
    ext: str
    content_type: str


async def _read_capped(upload: UploadFile) -> bytes:
    """Reads an UploadFile fully into memory, enforcing MAX_UPLOAD_BYTES."""
    chunks = []
    size = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "File too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_blob(upload: UploadFile, kind: str) -> _Blob:
    """Reads an upload and checks - from its actual bytes, not its filename or
    declared type - that it really is an image (kind="image") or an audio
    recording (kind="audio"). The extension and content type used from here on
    come from that detection, so an .html/.svg can never be stored or served
    as a "photo"."""
    data = await _read_capped(upload)
    sniffed = uploads.sniff_image(data) if kind == "image" else uploads.sniff_audio(data)
    if sniffed is None:
        raise HTTPException(415, f"The uploaded {'photo' if kind == 'image' else 'audio'} is not a supported file type.")
    return _Blob(data, sniffed[0], sniffed[1])


async def _save_blob(blob: _Blob, subdir: str, local_dir: str) -> str:
    """Saves a validated upload to Supabase Storage when configured (preferred -
    it persists across restarts/redeploys), otherwise falls back to local
    disk for local/dev use without a Supabase project set up. Returns what
    gets stored in photo_path/audio_path: a full public URL for Supabase
    Storage, or a path relative to DATA_DIR for local disk."""
    if USE_SUPABASE_STORAGE:
        try:
            return await storage.upload_bytes(blob.data, subdir, blob.ext, blob.content_type)
        except Exception as e:
            raise HTTPException(502, "Could not save the uploaded file. Please try again.") from e

    fname = f"{uuid.uuid4().hex}.{blob.ext}"
    dest = os.path.join(local_dir, fname)
    with open(dest, "wb") as out:
        out.write(blob.data)
    return os.path.relpath(dest, DATA_DIR)


def _apply_ai_result(report_id, existing_category, ai_result):
    """Stores an AI triage result onto a report. The AI's category only fills
    the category when it's still empty - a manually-set one is never
    overwritten."""
    if ai_result["status"] == "done":
        db.update_report_ai(
            report_id, "done",
            category=ai_result["category"] if existing_category is None else None,
            severity=ai_result["severity"],
            summary=ai_result["summary"],
            department=ai_result["department"],
        )
    else:
        db.update_report_ai(report_id, ai_result["status"])


@app.post("/api/reports")
async def create_report(
    category: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    # ge/le also reject nan/inf: one stored inf would make GET /api/reports
    # (and so the dashboard and the map) fail for everyone.
    lat: Optional[float] = Form(None, ge=-90, le=90),
    lng: Optional[float] = Form(None, ge=-180, le=180),
    accuracy: Optional[float] = Form(None, ge=0, le=1_000_000),
    location_label: Optional[str] = Form(None),
    photo: Optional[UploadFile] = File(None),
    audio: Optional[UploadFile] = File(None),
    quick_report: bool = Form(False),
):
    # Category is no longer chosen by the citizen in the form — it's left
    # unset here and is meant to be filled in later (e.g. by an AI
    # classification step reading the description/photo/audio), or by
    # someone triaging reports. If a caller does pass one (e.g. a future
    # admin tool), it's still validated against the known list.
    category = (category or "").strip() or None
    if category is not None and category not in CATEGORIES:
        raise HTTPException(400, f"Unknown category '{category}'")

    description = (description or "").strip()[:MAX_DESCRIPTION_CHARS] or None
    location_label = (location_label or "").strip()[:MAX_LOCATION_LABEL_CHARS] or None

    # A photo, a coordinate, and a street/neighborhood name are always
    # required. Beyond that, there are two ways to satisfy "what's the
    # problem": either a written description or a voice recording (the
    # original detailed form), or quick_report=true (the camera-first Quick
    # Report flow in frontend/index.html) - there the citizen is standing at
    # the problem when they tap the shutter, so the coordinate is captured
    # live (accurate) rather than pasted from Google Maps, and AI triage's
    # own summary stands in for a typed description instead of asking for
    # one. Enforced here too, not just in the frontend, since the frontend
    # check can be bypassed.
    has_photo = bool(photo and photo.filename)
    has_audio = bool(audio and audio.filename)
    if not has_photo:
        raise HTTPException(400, "A photo is required for every report.")
    if lat is None or lng is None:
        raise HTTPException(400, "A coordinate is required for every report.")
    if not location_label:
        raise HTTPException(400, "A street or neighborhood name is required for every report.")
    if not quick_report and not description and not has_audio:
        raise HTTPException(
            400,
            "Along with the photo, either a written description or a voice recording is required.",
        )

    # Validate both files before saving either, so a bad audio file can't
    # leave an orphaned photo behind.
    photo_blob = await _read_blob(photo, "image")
    audio_blob = await _read_blob(audio, "audio") if has_audio else None

    photo_path = await _save_blob(photo_blob, "photos", PHOTOS_DIR)
    audio_path = await _save_blob(audio_blob, "audio", AUDIO_DIR) if audio_blob else None

    report_id, created_at = db.insert_report(
        category, description, lat, lng, accuracy, location_label, photo_path, audio_path
    )

    # AI triage: reads the photo + description/location straight back through
    # Claude and returns a category/severity/summary/department, run here so
    # the citizen sees the result instantly in the same response - this is
    # deliberately synchronous (adds a few seconds to the request) rather
    # than a background job, since immediate feedback is the point of the
    # feature for a live demo. It can never fail the submission itself: both
    # the AI call and the DB update below are wrapped so the report a citizen
    # just submitted is always saved and acknowledged, AI or no AI.
    try:
        ai_result = await ai.analyze_report(
            description, location_label, photo_path, image_bytes=photo_blob.data
        )
    except Exception:
        ai_result = {"status": "failed", "category": None, "severity": None, "summary": None, "department": None}

    try:
        _apply_ai_result(report_id, category, ai_result)
    except Exception:
        logger.exception("could not store AI triage result for report %s", report_id)
        # report itself is already saved; the AI status update is best-effort

    final_category = category or (ai_result["category"] if ai_result["status"] == "done" else None)

    return JSONResponse(
        {
            "id": report_id,
            "created_at": created_at,
            "category": final_category,
            "category_label": CATEGORIES.get(final_category, "پۆلێنی نەکراو"),
            "description": description,
            "lat": lat,
            "lng": lng,
            "location_label": location_label,
            "photo_path": photo_path,
            "audio_path": audio_path,
            "ai_status": ai_result["status"],
            "severity": ai_result["severity"] if ai_result["status"] == "done" else None,
            "ai_summary": ai_result["summary"] if ai_result["status"] == "done" else None,
            "department": ai_result["department"] if ai_result["status"] == "done" else None,
        },
        status_code=201,
    )


@app.get("/api/geocode")
async def get_geocode(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
):
    """Reverse-geocode a point into a human-readable place name, used to
    pre-fill (but never lock) the location field in the report form."""
    label, source = await geocode.reverse_geocode(lat, lng)
    return {"label": label, "source": source}


@app.patch("/api/reports/{report_id}/media", dependencies=admin_only)
async def update_report_media(
    report_id: int,
    photo: Optional[UploadFile] = File(None),
    audio: Optional[UploadFile] = File(None),
):
    """Admin recovery endpoint: re-attaches a photo and/or audio recording to
    an existing report. Exists for cases like a report whose photo was lost
    when it was still being saved to Render's non-persistent local disk
    (before Supabase Storage was wired in) - the original submitter's photo
    can be re-uploaded here and it'll be saved to Supabase Storage and
    linked back onto that same report id, with everything else about the
    report (description, location, timestamp) left untouched."""
    has_photo = bool(photo and photo.filename)
    has_audio = bool(audio and audio.filename)
    if not has_photo and not has_audio:
        raise HTTPException(400, "Provide a photo and/or an audio file to attach.")

    photo_blob = await _read_blob(photo, "image") if has_photo else None
    audio_blob = await _read_blob(audio, "audio") if has_audio else None

    if db.get_report(report_id) is None:
        raise HTTPException(404, f"No report with id {report_id}")

    photo_path = await _save_blob(photo_blob, "photos", PHOTOS_DIR) if photo_blob else None
    audio_path = await _save_blob(audio_blob, "audio", AUDIO_DIR) if audio_blob else None

    updated = db.update_report_media(report_id, photo_path=photo_path, audio_path=audio_path)
    if not updated:
        raise HTTPException(404, f"No report with id {report_id}")

    return {"id": report_id, "photo_path": photo_path, "audio_path": audio_path}


@app.post("/api/reports/{report_id}/triage", dependencies=admin_only)
async def triage_report(report_id: int):
    """(Re-)runs AI triage on an existing report using its already-stored
    photo/description/location. Lets the dashboard backfill AI results onto
    reports submitted before this feature existed (e.g. the reports from
    initial data collection), or retry one that came back 'failed' the
    first time (a transient API hiccup, a slow image download, etc.)."""
    report = db.get_report(report_id)
    if not report:
        raise HTTPException(404, f"No report with id {report_id}")

    ai_result = await ai.analyze_report(report["description"], report["location_label"], report["photo_path"])
    _apply_ai_result(report_id, report["category"], ai_result)

    return {"id": report_id, **ai_result}


@app.post("/api/reports/triage-all", dependencies=admin_only)
async def triage_all_reports(only_pending: bool = True):
    """Bulk-runs AI triage over stored reports - the one-click way to
    backfill AI results onto every report collected before this feature
    existed. Runs sequentially (not in parallel) to stay comfortably under
    Anthropic's rate limits; fine at hackathon volume (tens of reports).
    only_pending=true (the default) skips reports whose last triage already
    succeeded, so re-running this after adding a few new reports doesn't
    re-spend API calls on ones already done."""
    ids = db.list_report_ids(exclude_status="done") if only_pending else db.list_report_ids()
    results = []
    for report_id in ids:
        report = db.get_report(report_id)
        if not report:
            continue
        ai_result = await ai.analyze_report(report["description"], report["location_label"], report["photo_path"])
        _apply_ai_result(report_id, report["category"], ai_result)
        results.append({"id": report_id, "status": ai_result["status"]})
    return {"processed": len(results), "results": results}


@app.get("/api/map")
def get_map_reports():
    """Public data for map.html: only reports with a location, and only the
    fields the map popup shows (no raw description, audio, or GPS accuracy)."""
    return {"total": db.count_reports(), "reports": db.list_map_reports()}


@app.get("/api/reports", dependencies=admin_only)
def get_reports(limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0)):
    return {"total": db.count_reports(), "reports": db.list_reports(limit, offset)}


@app.get("/api/reports/stats", dependencies=admin_only)
def get_stats():
    return db.stats()


@app.get("/api/reports/export.csv", dependencies=admin_only)
def export_csv():
    return Response(
        db.export_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="reports.csv"'},
    )


@app.get("/media/{kind}/{filename}")
def get_media(kind: str, filename: str):
    if kind not in ("photos", "audio"):
        raise HTTPException(404)
    base = os.path.realpath(os.path.join(DATA_DIR, "uploads", kind))
    path = os.path.realpath(os.path.join(base, filename))
    if not path.startswith(base + os.sep) or not os.path.isfile(path):
        raise HTTPException(404)
    return FileResponse(path, headers={"X-Content-Type-Options": "nosniff"})


# Serve the Kurdish frontend (index.html + any static assets) at "/".
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

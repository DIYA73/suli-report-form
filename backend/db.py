# -*- coding: utf-8 -*-
"""
Storage layer for reports.

Works against SQLite (zero-setup local/demo use — the default) or a real
Postgres server (set the DATABASE_URL env var, e.g. to a free Supabase
project) with the same code, via SQLAlchemy Core. This matters once the
form is deployed publicly: most free app-hosting tiers (Render, Railway,
etc.) wipe local disk on every restart/redeploy, so SQLite on those hosts
silently loses data — pointing DATABASE_URL at a real Postgres server
avoids that.
"""

import csv
import io
import math
import os
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from config import DB_PATH, CATEGORIES

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

if DATABASE_URL:
    # Render/Heroku-style URLs sometimes use the old "postgres://" scheme;
    # SQLAlchemy's psycopg2 driver wants "postgresql://".
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    ENGINE_URL = DATABASE_URL
    # DATABASE_URL=sqlite:///... is also allowed (handy for tests/local dev).
    IS_POSTGRES = not DATABASE_URL.startswith("sqlite")
else:
    ENGINE_URL = f"sqlite:///{DB_PATH}"
    IS_POSTGRES = False

if IS_POSTGRES:
    _connect_args = {}
    if make_url(ENGINE_URL).get_driver_name() == "psycopg":
        # A plain postgresql:// URL uses psycopg 3 on current SQLAlchemy. psycopg 3
        # silently switches to server-side prepared statements once the same query
        # has run a few times on a connection, and Supabase's transaction pooler
        # (port 6543, the one the README recommends) does not support those -
        # that shows up as random 'prepared statement ... does not exist' errors
        # under load. prepare_threshold=None turns the feature off (harmless on
        # direct connections too).
        _connect_args["prepare_threshold"] = None
    # Free-tier hosting (Render + Supabase's pooler) silently drops idle
    # connections. Without pre-ping the first request after a quiet spell
    # fails with "server closed the connection unexpectedly" (a 500 for the
    # citizen); pre-ping checks the connection first and reconnects if needed.
    engine = create_engine(
        ENGINE_URL, future=True, pool_pre_ping=True, pool_recycle=300, connect_args=_connect_args
    )
else:
    engine = create_engine(ENGINE_URL, future=True)

if IS_POSTGRES:
    ID_COLUMN = "id SERIAL PRIMARY KEY"
else:
    ID_COLUMN = "id INTEGER PRIMARY KEY AUTOINCREMENT"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS reports (
    {ID_COLUMN},
    created_at      TEXT NOT NULL,
    category        TEXT,
    description     TEXT,
    lat             DOUBLE PRECISION,
    lng             DOUBLE PRECISION,
    location_accuracy DOUBLE PRECISION,
    location_label  TEXT,
    photo_path      TEXT,
    audio_path      TEXT,
    status          TEXT NOT NULL DEFAULT 'new',
    severity        INTEGER,
    ai_summary      TEXT,
    department      TEXT,
    ai_status       TEXT NOT NULL DEFAULT 'pending',
    CHECK (description IS NOT NULL OR photo_path IS NOT NULL OR audio_path IS NOT NULL)
);
""" if IS_POSTGRES else """
CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    category        TEXT,
    description     TEXT,
    lat             REAL,
    lng             REAL,
    location_accuracy REAL,
    location_label  TEXT,
    photo_path      TEXT,
    audio_path      TEXT,
    status          TEXT NOT NULL DEFAULT 'new',
    severity        INTEGER,
    ai_summary      TEXT,
    department      TEXT,
    ai_status       TEXT NOT NULL DEFAULT 'pending',
    CHECK (description IS NOT NULL OR photo_path IS NOT NULL OR audio_path IS NOT NULL)
);
"""


def init_db():
    with engine.begin() as conn:
        conn.execute(text(SCHEMA))
    # Best-effort migrations for databases created before certain columns
    # existed (notably the already-live production DB, which predates the
    # AI triage columns below). Each column gets its own transaction so a
    # "column already exists" error on one never blocks the others or rolls
    # back the CREATE TABLE above.
    migrations = [
        ("location_label", "TEXT"),
        ("severity", "INTEGER"),
        ("ai_summary", "TEXT"),
        ("department", "TEXT"),
        ("ai_status", "TEXT NOT NULL DEFAULT 'pending'"),
    ]
    for column, col_type in migrations:
        try:
            with engine.begin() as conn:
                if IS_POSTGRES:
                    conn.execute(text(f"ALTER TABLE reports ADD COLUMN IF NOT EXISTS {column} {col_type}"))
                else:
                    conn.execute(text(f"ALTER TABLE reports ADD COLUMN {column} {col_type}"))
        except Exception:
            pass


def _clean_row(row):
    """JSON can't represent inf/nan, and one such value in a single row would
    make GET /api/reports (dashboard + map) fail for everybody. New input is
    validated in main.py, but this keeps any already-stored bad row harmless."""
    d = dict(row)
    for key in ("lat", "lng", "location_accuracy"):
        v = d.get(key)
        if v is not None and not math.isfinite(v):
            d[key] = None
    return d


def insert_report(category, description, lat, lng, accuracy, location_label, photo_path, audio_path):
    created_at = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                INSERT INTO reports
                    (created_at, category, description, lat, lng, location_accuracy, location_label,
                     photo_path, audio_path, status)
                VALUES
                    (:created_at, :category, :description, :lat, :lng, :accuracy, :location_label,
                     :photo_path, :audio_path, 'new')
                RETURNING id
                """
            ),
            {
                "created_at": created_at, "category": category, "description": description,
                "lat": lat, "lng": lng, "accuracy": accuracy, "location_label": location_label,
                "photo_path": photo_path, "audio_path": audio_path,
            },
        ).fetchone()
        return row[0], created_at


def list_reports(limit=200, offset=0):
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, created_at, category, description, lat, lng, location_accuracy, location_label,
                       photo_path, audio_path, status, severity, ai_summary, department, ai_status
                FROM reports
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"limit": limit, "offset": offset},
        ).mappings().all()
        return [_clean_row(r) for r in rows]


def list_map_reports(limit=1000):
    """Minimal columns for the PUBLIC map page: only reports that have a valid
    location, and none of the raw citizen-typed description, audio, or GPS
    accuracy. (GET /api/reports - the full rows - is the admin/dashboard view.)"""
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, created_at, category, lat, lng, location_label, photo_path,
                       severity, ai_summary, department, ai_status
                FROM reports
                WHERE lat BETWEEN -90 AND 90 AND lng BETWEEN -180 AND 180
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).mappings().all()
        return [dict(r) for r in rows]


def get_report(report_id):
    """A single report's full row, including AI fields - used by the
    (re-)triage endpoints to read back the description/location/photo_path
    they need to feed to ai.analyze_report."""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, created_at, category, description, lat, lng, location_accuracy, location_label,
                       photo_path, audio_path, status, severity, ai_summary, department, ai_status
                FROM reports WHERE id = :id
                """
            ),
            {"id": report_id},
        ).mappings().first()
        return _clean_row(row) if row else None


def list_report_ids(exclude_status=None):
    """Returns all report ids, oldest first, optionally excluding ones whose
    ai_status already equals exclude_status - used by the bulk-triage
    endpoint to skip reports already successfully triaged."""
    with engine.begin() as conn:
        if exclude_status:
            rows = conn.execute(
                text("SELECT id FROM reports WHERE ai_status IS NULL OR ai_status != :s ORDER BY id"),
                {"s": exclude_status},
            ).all()
        else:
            rows = conn.execute(text("SELECT id FROM reports ORDER BY id")).all()
        return [r[0] for r in rows]


def update_report_ai(report_id, status, category=None, severity=None, summary=None, department=None):
    """Stores an AI triage result onto an existing report. `category` reuses
    the existing `category` column - it was always meant to be filled in by
    an AI classification step (see config.py/README) rather than getting a
    duplicate ai_category column - and is only touched when the AI actually
    returned one, so a manually-set category is never clobbered by a
    failed/uncertain AI call. Returns True if a row matched."""
    sets = ["ai_status = :status"]
    params = {"id": report_id, "status": status}
    if category is not None:
        sets.append("category = :category")
        params["category"] = category
    if severity is not None:
        sets.append("severity = :severity")
        params["severity"] = severity
    if summary is not None:
        sets.append("ai_summary = :summary")
        params["summary"] = summary
    if department is not None:
        sets.append("department = :department")
        params["department"] = department
    with engine.begin() as conn:
        result = conn.execute(
            text(f"UPDATE reports SET {', '.join(sets)} WHERE id = :id"),
            params,
        )
        return result.rowcount > 0


def update_report_media(report_id, photo_path=None, audio_path=None):
    """Overwrites photo_path and/or audio_path on an existing report - used
    by the admin "re-attach media" endpoint to recover a report whose photo
    was lost before Supabase Storage was wired in (see storage.py). Only
    columns actually passed in are touched. Returns True if a row matched."""
    sets = []
    params = {"id": report_id}
    if photo_path is not None:
        sets.append("photo_path = :photo_path")
        params["photo_path"] = photo_path
    if audio_path is not None:
        sets.append("audio_path = :audio_path")
        params["audio_path"] = audio_path
    if not sets:
        return False
    with engine.begin() as conn:
        result = conn.execute(
            text(f"UPDATE reports SET {', '.join(sets)} WHERE id = :id"),
            params,
        )
        return result.rowcount > 0


def count_reports():
    with engine.begin() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM reports")).scalar()


def stats():
    """Aggregate counts for the dashboard."""
    with engine.begin() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM reports")).scalar()
        with_photo = conn.execute(
            text("SELECT COUNT(*) FROM reports WHERE photo_path IS NOT NULL")
        ).scalar()
        with_audio = conn.execute(
            text("SELECT COUNT(*) FROM reports WHERE audio_path IS NOT NULL")
        ).scalar()
        with_location = conn.execute(
            text("SELECT COUNT(*) FROM reports WHERE lat IS NOT NULL")
        ).scalar()

        by_category_rows = conn.execute(
            text("SELECT category, COUNT(*) AS n FROM reports GROUP BY category ORDER BY n DESC")
        ).all()
        by_category = [
            {
                "category": r[0] or "unclassified",
                "label": CATEGORIES.get(r[0], "پۆلێنی نەکراو") if r[0] else "پۆلێنی نەکراو",
                "count": r[1],
            }
            for r in by_category_rows
        ]

        # last 14 days, oldest -> newest, using the date portion of the ISO created_at string
        date_expr = "substr(created_at, 1, 10)"
        by_day_rows = conn.execute(
            text(f"""
                SELECT {date_expr} AS day, COUNT(*) AS n
                FROM reports
                GROUP BY day
                ORDER BY day DESC
                LIMIT 14
            """)
        ).all()
        by_day = sorted([{"day": r[0], "count": r[1]} for r in by_day_rows], key=lambda x: x["day"])

        triaged = conn.execute(text("SELECT COUNT(*) FROM reports WHERE ai_status = 'done'")).scalar()
        avg_severity = conn.execute(
            text("SELECT AVG(severity) FROM reports WHERE severity IS NOT NULL")
        ).scalar()

        by_department_rows = conn.execute(
            text(
                "SELECT department, COUNT(*) AS n FROM reports "
                "WHERE department IS NOT NULL GROUP BY department ORDER BY n DESC"
            )
        ).all()
        by_department = [{"department": r[0], "count": r[1]} for r in by_department_rows]

    return {
        "total": total,
        "with_photo": with_photo,
        "with_audio": with_audio,
        "with_location": with_location,
        "by_category": by_category,
        "by_day": by_day,
        "triaged": triaged,
        "avg_severity": round(avg_severity, 1) if avg_severity is not None else None,
        "by_department": by_department,
    }


# Spreadsheet apps treat a cell starting with one of these as a formula. Citizens
# control description/location text, so prefix those cells with ' to keep them
# plain text (the standard CSV-injection mitigation).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value):
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def export_csv():
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, created_at, category, description, lat, lng, location_accuracy, location_label,
                       photo_path, audio_path, status, severity, ai_summary, department, ai_status
                FROM reports
                ORDER BY id ASC
                """
            )
        ).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["id", "created_at", "category", "category_label", "description",
         "lat", "lng", "location_accuracy", "location_label", "photo_path", "audio_path", "status",
         "severity", "ai_summary", "department", "ai_status"]
    )
    for r in rows:
        category_label = CATEGORIES.get(r[2], "پۆلێنی نەکراو") if r[2] else "پۆلێنی نەکراو"
        writer.writerow([_csv_safe(v) for v in (
            r[0], r[1], r[2], category_label, r[3],
            r[4], r[5], r[6], r[7], r[8], r[9], r[10],
            r[11], r[12], r[13], r[14],
        )])
    # Leading BOM so Excel opens the file as UTF-8 (otherwise Kurdish/Arabic
    # text shows up as garbage).
    return "\ufeff" + buf.getvalue()

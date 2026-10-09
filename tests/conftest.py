# -*- coding: utf-8 -*-
"""Shared test setup: an isolated data folder + SQLite database and no real API
keys, so the suite never touches real data or the network."""

import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="report-form-tests-")
os.environ["DATA_DIR"] = _TMP
for _var in ("DATABASE_URL", "ANTHROPIC_API_KEY", "ADMIN_TOKEN",
             "SUPABASE_URL", "SUPABASE_SERVICE_KEY", "GOOGLE_MAPS_API_KEY"):
    os.environ.pop(_var, None)

# By default the suite runs against a throwaway SQLite file. To run it against
# Postgres too (what production uses), set TEST_DATABASE_URL. The tests delete
# every row in `reports`, so refuse anything that isn't clearly a test database.
_TEST_DB = os.environ.get("TEST_DATABASE_URL", "").strip()
if _TEST_DB:
    if "test" not in _TEST_DB.rsplit("/", 1)[-1].split("?")[0].lower():
        raise RuntimeError(
            "TEST_DATABASE_URL must point at a database whose name contains 'test' "
            "(the tests wipe the reports table)."
        )
    os.environ["DATABASE_URL"] = _TEST_DB

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import ai  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import main  # noqa: E402

# Smallest byte strings the type detection accepts.
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 32
M4A = b"\x00\x00\x00\x1cftypM4A " + b"\x00" * 32
HTML = b"<html><script>alert(1)</script></html>"

db.init_db()


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Every test starts with an empty table, open admin mode, and no AI key."""
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM reports"))
    monkeypatch.setattr(config, "ADMIN_TOKEN", "")
    monkeypatch.setattr(ai, "ANTHROPIC_API_KEY", "")


@pytest.fixture()
def client():
    with TestClient(main.app) as c:
        yield c


def post_report(client, photo=("photo.jpg", JPEG, "image/jpeg"), audio=None, **form):
    data = {"description": "pothole", "lat": "35.5608", "lng": "45.4347", "location_label": "Raparin"}
    data.update(form)
    files = {}
    if photo:
        files["photo"] = photo
    if audio:
        files["audio"] = audio
    return client.post("/api/reports", data=data, files=files or None)


def report_count():
    with db.engine.begin() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM reports")).scalar()

# -*- coding: utf-8 -*-
import csv
import io
import os

import pytest
from sqlalchemy import text

import ai
import config
import db
from conftest import HTML, JPEG, M4A, PNG, WEBM, post_report, report_count


# ---------------------------------------------------------------- basics

def test_health_and_config(client):
    assert client.get("/api/health").json() == {"status": "ok", "reports": 0}
    assert "categories" in client.get("/api/config").json()


def test_create_report_happy_path(client):
    r = post_report(client)
    assert r.status_code == 201
    body = r.json()
    assert body["photo_path"].endswith(".jpg")
    assert body["lat"] == 35.5608 and body["location_label"] == "Raparin"
    assert body["ai_status"] == "unavailable"  # no API key in tests
    listed = client.get("/api/reports").json()
    assert listed["total"] == 1 and listed["reports"][0]["id"] == body["id"]


@pytest.mark.parametrize("override,expected", [
    ({"photo": None}, (400,)),                    # photo is required
    ({"lat": ""}, (400, 422)),                    # coordinate is required (older FastAPI answers 422 for "")
    ({"lng": ""}, (400, 422)),
    ({"location_label": "  "}, (400,)),          # street/neighborhood is required
    ({"description": ""}, (400,)),                # description or audio is required...
    ({"category": "not-a-category"}, (400,)),
])
def test_missing_or_invalid_required_fields(client, override, expected):
    assert post_report(client, **override).status_code in expected
    assert report_count() == 0


def test_quick_report_only_needs_photo_location_and_label(client):
    r = post_report(client, description="", quick_report="true")
    assert r.status_code == 201


def test_long_text_is_truncated_not_rejected(client):
    r = post_report(client, description="x" * 5000, location_label="y" * 500)
    assert r.status_code == 201
    body = r.json()
    assert len(body["description"]) == config.MAX_DESCRIPTION_CHARS
    assert len(body["location_label"]) == config.MAX_LOCATION_LABEL_CHARS


# ------------------------------------------------- coordinate validation

@pytest.mark.parametrize("field,value", [
    ("lat", "inf"), ("lat", "-inf"), ("lat", "nan"), ("lat", "91"), ("lat", "-91"),
    ("lng", "inf"), ("lng", "nan"), ("lng", "181"), ("lng", "-181"),
    ("accuracy", "-1"), ("accuracy", "inf"),
])
def test_bad_numbers_are_rejected_and_nothing_is_stored(client, field, value):
    r = post_report(client, **{field: value})
    assert r.status_code == 422
    assert report_count() == 0


def test_a_stored_non_finite_row_cannot_break_the_dashboard_or_map(client):
    """Regression: one row with lat=inf used to make GET /api/reports return a
    500 for everybody (JSON can't encode inf)."""
    post_report(client)
    with db.engine.begin() as conn:
        conn.execute(text("UPDATE reports SET lat = :v"), {"v": float("inf")})
    r = client.get("/api/reports")
    assert r.status_code == 200 and r.json()["reports"][0]["lat"] is None
    assert client.get("/api/map").json()["reports"] == []  # not pinned anywhere
    assert client.get("/api/reports/stats").status_code == 200


# ------------------------------------------------------- upload checks

def test_a_non_image_cannot_be_uploaded_as_the_photo(client):
    r = post_report(client, photo=("evil.html", HTML, "text/html"))
    assert r.status_code == 415
    r = post_report(client, photo=("fake.jpg", b"this is not a jpeg", "image/jpeg"))
    assert r.status_code == 415
    assert report_count() == 0


def test_stored_extension_comes_from_the_bytes_not_the_filename(client):
    r = post_report(client, photo=("evil.html", JPEG, "text/html"))
    assert r.status_code == 201
    assert r.json()["photo_path"].endswith(".jpg")
    r = post_report(client, photo=("x.svg", PNG, "image/svg+xml"))
    assert r.json()["photo_path"].endswith(".png")


def test_audio_must_be_real_audio_and_keeps_its_real_type(client):
    ok = post_report(client, description="", audio=("voice-note.webm", WEBM, "audio/webm"))
    assert ok.status_code == 201 and ok.json()["audio_path"].endswith(".webm")
    ios = post_report(client, description="", audio=("voice-note.webm", M4A, "audio/webm"))
    assert ios.status_code == 201 and ios.json()["audio_path"].endswith(".m4a")  # Safari/iOS recordings
    bad = post_report(client, audio=("voice-note.webm", HTML, "audio/webm"))
    assert bad.status_code == 415


def test_invalid_audio_does_not_leave_an_orphan_photo_behind(client):
    photos = os.path.join(config.DATA_DIR, "uploads", "photos")
    before = set(os.listdir(photos))
    assert post_report(client, audio=("v.webm", HTML, "audio/webm")).status_code == 415
    assert set(os.listdir(photos)) == before


def test_oversized_upload_is_rejected(client, monkeypatch):
    monkeypatch.setattr("main.MAX_UPLOAD_BYTES", 1024)
    r = post_report(client, photo=("big.jpg", JPEG + b"\x00" * 2048, "image/jpeg"))
    assert r.status_code == 413


# ------------------------------------------------------------ AI wiring

def test_ai_result_is_stored_and_returned(client, monkeypatch):
    seen = {}

    async def fake(description, location_label, photo_path, image_bytes=None):
        seen.update(description=description, image_bytes=image_bytes)
        return {"status": "done", "category": next(iter(config.CATEGORIES)), "severity": 4,
                "summary": "Deep pothole", "department": "Roads & Maintenance"}

    monkeypatch.setattr("main.ai.analyze_report", fake)
    body = post_report(client).json()
    assert body["ai_status"] == "done" and body["severity"] == 4
    assert body["department"] == "Roads & Maintenance"
    assert seen["image_bytes"] == JPEG  # reuses the upload instead of downloading it again
    stored = client.get("/api/reports").json()["reports"][0]
    assert stored["ai_status"] == "done" and stored["severity"] == 4


def test_a_crashing_ai_never_loses_the_report(client, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("anthropic is down")

    monkeypatch.setattr("main.ai.analyze_report", boom)
    r = post_report(client)
    assert r.status_code == 201 and r.json()["ai_status"] == "failed"
    assert report_count() == 1


def test_triage_endpoints(client, monkeypatch):
    first_category = next(iter(config.CATEGORIES))
    calls = []

    async def fake(description, location_label, photo_path, image_bytes=None):
        calls.append(photo_path)
        return {"status": "done", "category": first_category, "severity": 2,
                "summary": "ok", "department": "Sanitation"}

    a = post_report(client).json()["id"]
    b = post_report(client).json()["id"]
    monkeypatch.setattr("main.ai.analyze_report", fake)

    assert client.post(f"/api/reports/{a}/triage").json()["status"] == "done"
    assert client.post("/api/reports/9999/triage").status_code == 404
    # only_pending skips the one that is already done...
    assert client.post("/api/reports/triage-all").json()["processed"] == 1
    # ...while only_pending=false redoes everything
    assert client.post("/api/reports/triage-all?only_pending=false").json()["processed"] == 2
    row = db.get_report(b)
    assert row["category"] == first_category and row["department"] == "Sanitation"


# ------------------------------------------------------- media re-attach

def test_patch_media_replaces_the_photo(client):
    rid = post_report(client).json()["id"]
    r = client.patch(f"/api/reports/{rid}/media", files={"photo": ("new.png", PNG, "image/png")})
    assert r.status_code == 200 and r.json()["photo_path"].endswith(".png")
    assert db.get_report(rid)["photo_path"] == r.json()["photo_path"]
    assert client.patch(f"/api/reports/{rid}/media", files={"photo": ("e.html", HTML, "text/html")}).status_code == 415
    assert client.patch(f"/api/reports/{rid}/media").status_code == 400
    assert client.patch("/api/reports/9999/media", files={"photo": ("n.png", PNG, "image/png")}).status_code == 404


# ------------------------------------------------------------- media route

def test_media_route_serves_files_safely(client):
    path = post_report(client).json()["photo_path"]  # uploads/photos/<id>.jpg
    r = client.get("/media/" + path.replace("uploads/", "", 1))
    assert r.status_code == 200 and r.content == JPEG
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["x-content-type-options"] == "nosniff"
    for bad in ("/media/photos/..%2F..%2Freports.db", "/media/photos/..", "/media/other/x.jpg",
                "/media/photos/missing.jpg", "/media/photos/%2e%2e%2f%2e%2e%2fetc%2fpasswd"):
        assert client.get(bad).status_code == 404, bad


def test_media_guard_blocks_traversal_even_if_routing_lets_a_name_through():
    """The URL router already refuses names containing '/', so this calls the
    handler directly to prove the folder check itself holds."""
    import main
    from fastapi import HTTPException

    secret = os.path.join(config.DATA_DIR, "secret.txt")  # outside uploads/, so it must stay unreachable
    with open(secret, "w") as f:
        f.write("top secret")
    for kind, name in [("photos", "../../secret.txt"), ("audio", "../photos/../../secret.txt")]:
        with pytest.raises(HTTPException) as excinfo:
            main.get_media(kind, name)
        assert excinfo.value.status_code == 404


# ------------------------------------------------------------- list limits

@pytest.mark.parametrize("query", ["limit=0", "limit=-1", "limit=1001", "offset=-1"])
def test_list_paging_is_bounded(client, query):
    assert client.get("/api/reports?" + query).status_code == 422


def test_list_paging_works(client):
    for _ in range(3):
        post_report(client)
    assert len(client.get("/api/reports?limit=2").json()["reports"]) == 2
    assert len(client.get("/api/reports?limit=2&offset=2").json()["reports"]) == 1


# --------------------------------------------------------------------- CSV

def test_csv_export_opens_cleanly_in_excel_and_cannot_run_formulas(client):
    post_report(client, description='=HYPERLINK("http://evil.example","click")', location_label="ڕاپەڕین")
    r = client.get("/api/reports/export.csv")
    assert r.status_code == 200
    assert r.content.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM so Excel shows Kurdish text correctly
    assert 'attachment; filename="reports.csv"' == r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    cells = rows[1]
    assert "ڕاپەڕین" in cells
    assert not any(c.startswith("=") for c in cells)
    assert any(c.startswith("'=HYPERLINK") for c in cells)


# ----------------------------------------------------- admin token gating

ADMIN_CALLS = [
    ("get", "/api/reports"),
    ("get", "/api/reports/stats"),
    ("get", "/api/reports/export.csv"),
    ("post", "/api/reports/1/triage"),
    ("post", "/api/reports/triage-all"),
    ("patch", "/api/reports/1/media"),
]


def _call(client, method, path, headers=None):
    kwargs = {"headers": headers or {}}
    if method == "patch":
        kwargs["files"] = {"photo": ("p.jpg", JPEG, "image/jpeg")}
    return getattr(client, method)(path, **kwargs)


@pytest.mark.parametrize("method,path", ADMIN_CALLS)
def test_admin_endpoints_are_locked_when_a_token_is_set(client, monkeypatch, method, path):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "s3cret")
    assert _call(client, method, path).status_code == 401
    assert _call(client, method, path, {"X-Admin-Token": "wrong"}).status_code == 401
    assert _call(client, method, path, {"X-Admin-Token": "s3cret"}).status_code != 401


@pytest.mark.parametrize("method,path", ADMIN_CALLS)
def test_admin_endpoints_stay_open_without_a_token_configured(client, method, path):
    assert _call(client, method, path).status_code != 401


def test_public_endpoints_never_need_the_token(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "s3cret")
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/config").status_code == 200
    assert client.get("/api/map").status_code == 200
    assert post_report(client).status_code == 201
    assert client.get("/").status_code == 200  # the form itself
    assert client.get("/common.js").status_code == 200


def test_public_map_data_leaves_out_private_fields(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "s3cret")
    post_report(client, description="call me on 0750 000 0000", audio=("v.webm", WEBM, "audio/webm"), accuracy="12")
    data = client.get("/api/map").json()
    assert data["total"] == 1
    pin = data["reports"][0]
    assert pin["location_label"] == "Raparin" and pin["lat"] == 35.5608
    for private in ("description", "audio_path", "location_accuracy"):
        assert private not in pin


# ----------------------------------------------------------------- geocode

@pytest.mark.parametrize("query", ["lat=999&lng=0", "lat=0&lng=999", "lat=nan&lng=0", "lat=inf&lng=0"])
def test_geocode_rejects_impossible_coordinates(client, query):
    assert client.get("/api/geocode?" + query).status_code == 422


# --------------------------------------------------------------- database

@pytest.mark.skipif(not db.IS_POSTGRES, reason="only relevant when running against Postgres")
def test_no_server_side_prepared_statements_are_created():
    """Supabase's transaction pooler can't handle them (psycopg 3 creates them
    automatically after a few repeats of a query), so they must stay off."""
    with db.engine.connect() as conn:
        for _ in range(12):
            conn.execute(text("SELECT id FROM reports WHERE id = :i"), {"i": 1})
        assert conn.execute(text("SELECT COUNT(*) FROM pg_prepared_statements")).scalar() == 0

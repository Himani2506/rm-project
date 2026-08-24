import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Point the app at a throwaway database before importing it.
_tmp = tempfile.mkdtemp()
os.environ["RM_DB_PATH"] = str(Path(_tmp) / "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from backend import db  # noqa: E402
from backend.main import app  # noqa: E402

def _auth(client, username, password):
    """Sign in and return an Authorization header for that account."""
    response = client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


ADMIN: dict = {}
STUDENT: dict = {}
REAL = ROOT / "data" / "sample_raw.csv"
DIRTY = ROOT / "tests" / "fixtures" / "dirty.csv"


@pytest.fixture(scope="module")
def client():
    db.reset()
    with TestClient(app) as c:
        ADMIN.update(_auth(c, "admin", "admin"))
        STUDENT.update(_auth(c, "student", "student"))
        with REAL.open("rb") as fh:
            response = c.post("/api/upload", files={"file": ("sample_raw.csv", fh, "text/csv")}, headers=ADMIN)
        assert response.status_code == 200, response.text
        yield c


def test_health(client):
    assert client.get("/api/health").json()["ok"] is True


def test_upload_requires_admin(client):
    with REAL.open("rb") as fh:
        response = client.post("/api/upload", files={"file": ("x.csv", fh, "text/csv")}, headers=STUDENT)
    assert response.status_code == 403


def test_upload_populated_the_database(client):
    stats = client.get("/api/stats", headers=ADMIN).json()
    assert stats["total_students"] == 99
    assert stats["quarantined"] == 0


def test_shortlist_threshold_matches_expected_counts(client):
    for threshold, expected in [(100, 80), (150, 52), (200, 15), (250, 3)]:
        stats = client.get("/api/stats", params={"min_total": threshold}, headers=ADMIN).json()
        assert stats["matched"] == expected, threshold


def test_debarred_student_leaves_the_shortlist(client):
    before = client.get("/api/stats", params={"min_total": 200}, headers=ADMIN).json()["matched"]
    top = client.get("/api/students", params={"min_total": 200, "shortlist_only": True},
                     headers=ADMIN).json()["students"][0]

    response = client.patch(f"/api/students/{top['id']}/status", json={"status": "debarred"}, headers=ADMIN)
    assert response.status_code == 200
    after = client.get("/api/stats", params={"min_total": 200}, headers=ADMIN).json()["matched"]
    assert after == before - 1

    client.patch(f"/api/students/{top['id']}/status", json={"status": "active"}, headers=ADMIN)
    assert client.get("/api/stats", params={"min_total": 200}, headers=ADMIN).json()["matched"] == before


def test_student_role_cannot_change_status(client):
    assert client.patch("/api/students/1/status", json={"status": "debarred"},
                        headers=STUDENT).status_code == 403
    assert client.patch("/api/students/1/status", json={"status": "debarred"}).status_code == 401


def test_student_role_sees_no_administrative_fields(client):
    rows = client.get("/api/students", headers=STUDENT).json()["students"]
    assert rows and "quarantine_reason" not in rows[0]
    assert all(r["status"] == "active" for r in rows)


def test_export_returns_csv_with_matching_row_count(client):
    response = client.get("/api/export", params={"min_total": 200}, headers=ADMIN)
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    lines = [line for line in response.text.strip().splitlines() if line]
    assert len(lines) - 1 == client.get("/api/stats", params={"min_total": 200}, headers=ADMIN).json()["matched"]


def test_export_requires_admin(client):
    assert client.get("/api/export", headers=STUDENT).status_code == 403


def test_bulk_debar_and_audit_trail(client):
    ids = [s["id"] for s in client.get("/api/students", headers=ADMIN).json()["students"][:3]]
    response = client.patch("/api/students/status", json={"ids": ids, "status": "debarred"}, headers=ADMIN)
    assert response.status_code == 200
    assert client.get("/api/stats", headers=ADMIN).json()["debarred"] >= 3

    audit = client.get("/api/audit", headers=ADMIN).json()["entries"]
    assert audit and audit[0]["actor_role"] == "admin"

    client.patch("/api/students/status", json={"ids": ids, "status": "active"}, headers=ADMIN)


def test_websocket_receives_status_broadcast(client):
    ticket = client.post("/api/ws-ticket", headers=ADMIN).json()["ticket"]
    with client.websocket_connect(f"/ws?ticket={ticket}") as ws:
        ws.receive_json()  # presence frame on connect
        student_id = client.get("/api/students", headers=ADMIN).json()["students"][0]["id"]
        client.patch(f"/api/students/{student_id}/status", json={"status": "debarred"}, headers=ADMIN)
        message = ws.receive_json()
        assert message["type"] == "status_changed"
        assert message["student"]["status"] == "debarred"
        assert "matched" in message["stats"]
        client.patch(f"/api/students/{student_id}/status", json={"status": "active"}, headers=ADMIN)


def test_quarantined_rows_excluded_from_shortlist_after_dirty_upload(client):
    with DIRTY.open("rb") as fh:
        client.post("/api/upload", files={"file": ("dirty.csv", fh, "text/csv")}, headers=ADMIN)
    stats = client.get("/api/stats", headers=ADMIN).json()
    assert stats["quarantined"] >= 2
    assert stats["eligible_pool"] == stats["total_students"] - stats["quarantined"] - stats["debarred"]

    rejects = client.get("/api/export/rejects", headers=ADMIN)
    assert rejects.status_code == 200
    assert len(rejects.text.strip().splitlines()) - 1 == stats["quarantined"]


def test_unscoreable_file_prompts_for_mapping_rather_than_failing(client):
    """A file with no recognisable score column is not an error.

    It is a file the application cannot map on its own, so it asks.
    """
    response = client.post(
        "/api/upload",
        files={"file": ("bad.csv", b"Foo,Bar\n1,2\n", "text/csv")},
        headers=ADMIN,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["needs_mapping"] is True
    assert body["reason"]


def test_mapping_with_no_score_column_is_rejected(client):
    """Once the admin has chosen, an empty choice is a real error."""
    response = client.post(
        "/api/upload",
        files={"file": ("bad.csv", b"Foo,Bar\n1,2\n", "text/csv")},
        data={"mapping": json.dumps({"name": "Foo", "subjects": []})},
        headers=ADMIN,
    )
    assert response.status_code == 422
    assert "at least one numeric column" in response.json()["detail"]


def test_empty_file_is_rejected(client):
    response = client.post(
        "/api/upload", files={"file": ("empty.csv", b"", "text/csv")}, headers=ADMIN
    )
    assert response.status_code == 400


def test_inspect_returns_columns_and_a_suggestion(client):
    with REAL.open("rb") as fh:
        r = client.post("/api/inspect", files={"file": ("sample_raw.csv", fh, "text/csv")}, headers=ADMIN)
    body = r.json()
    assert body["row_count"] == 99
    assert body["suggested"]["subjects"] == ["Math", "Science", "English"]
    assert len(body["preview"]) == 5
    assert body["confident"] is True


def test_unmappable_file_asks_for_a_mapping_instead_of_failing(client):
    csv = b"school,guardian,note\nGP,mother,hello\nGP,father,there\n"
    r = client.post("/api/upload", files={"file": ("odd.csv", csv, "text/csv")}, headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["needs_mapping"] is True
    assert {c["column"] for c in body["columns"]} == {"school", "guardian", "note"}


def test_upload_accepts_an_explicit_mapping(client):
    csv = b"who,alpha,beta\nAsha,50,60\nBela,70,80\nCara,90,95\n"
    mapping = json.dumps({"name": "who", "gender": None, "grade": None,
                          "total": None, "subjects": ["alpha", "beta"]})
    r = client.post("/api/upload", files={"file": ("m.csv", csv, "text/csv")},
                    data={"mapping": mapping}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["report"]["subject_labels"] == ["alpha", "beta"]

    stats = client.get("/api/stats", headers=ADMIN).json()
    assert stats["subject_labels"] == ["alpha", "beta"]
    assert stats["total_students"] == 3

    export = client.get("/api/export", params={"min_total": 0}, headers=ADMIN).text
    assert export.splitlines()[0] == "name,gender,grade,alpha,beta,total"

    # restore the assessment dataset for any later test
    with REAL.open("rb") as fh:
        client.post("/api/upload", files={"file": ("sample_raw.csv", fh, "text/csv")}, headers=ADMIN)


# --- authentication -------------------------------------------------------

def test_login_rejects_wrong_password(client):
    assert client.post("/api/login", json={"username": "admin", "password": "nope"}).status_code == 401
    body = client.post("/api/login", json={"username": "admin", "password": "admin"}).json()
    assert body["role"] == "admin" and body["token"]


def test_unknown_user_and_wrong_password_are_indistinguishable(client):
    a = client.post("/api/login", json={"username": "nobody", "password": "x"})
    b = client.post("/api/login", json={"username": "admin", "password": "x"})
    assert a.status_code == b.status_code == 401
    assert a.json()["detail"] == b.json()["detail"]


def test_passwords_are_never_stored_in_plaintext():
    from backend.auth import USERS
    for record in USERS.values():
        assert "password" not in record
        assert len(record["hash"]) == 64 and len(record["salt"]) == 32
        assert record["hash"] != "admin"


def test_a_tampered_token_is_rejected(client):
    token = client.post("/api/login", json={"username": "student", "password": "student"}).json()["token"]
    body, signature = token.split(".", 1)

    # Same signature, altered claims.
    import base64, json as _json
    claims = _json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims["role"] = "admin"
    forged_body = base64.urlsafe_b64encode(
        _json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    ).decode().rstrip("=")
    forged = f"{forged_body}.{signature}"

    assert client.get("/api/export", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_an_expired_token_is_rejected(client):
    from backend.auth import issue_token
    stale = issue_token("admin", "admin", ttl=-1)
    assert client.get("/api/export", headers={"Authorization": f"Bearer {stale}"}).status_code == 401


def test_a_student_token_cannot_be_used_for_admin_routes(client):
    for method, url in [("get", "/api/export"), ("get", "/api/audit"), ("get", "/api/cleaning-log")]:
        assert getattr(client, method)(url, headers=STUDENT).status_code == 403


def test_garbage_authorization_headers_are_rejected(client):
    for value in ["", "Bearer", "Bearer ", "Bearer notatoken", "Basic YWRtaW46YWRtaW4=", "abc.def"]:
        assert client.get("/api/export", headers={"Authorization": value}).status_code == 401


def test_a_session_token_cannot_be_reused_as_a_websocket_ticket(client):
    session = client.post("/api/login", json={"username": "admin", "password": "admin"}).json()["token"]
    with pytest.raises(Exception):
        with client.websocket_connect(f"/ws?ticket={session}"):
            pass


def test_websocket_requires_a_valid_ticket(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws"):
            pass

    ticket = client.post("/api/ws-ticket", headers=ADMIN).json()["ticket"]
    with client.websocket_connect(f"/ws?ticket={ticket}") as ws:
        assert ws.receive_json()["type"] == "presence"


def test_ws_ticket_needs_a_session(client):
    assert client.post("/api/ws-ticket").status_code == 401


def test_audit_records_the_username_not_the_role(client):
    student_id = client.get("/api/students", headers=ADMIN).json()["students"][0]["id"]
    client.patch(f"/api/students/{student_id}/status", json={"status": "debarred"}, headers=ADMIN)
    entry = client.get("/api/audit", headers=ADMIN).json()["entries"][0]
    assert entry["actor_role"] == "admin"
    client.patch(f"/api/students/{student_id}/status", json={"status": "active"}, headers=ADMIN)


# --- hardening ------------------------------------------------------------

def test_static_handler_cannot_escape_the_bundle(client):
    """A relative path must not be able to reach application source."""
    import backend.main as api
    spa = getattr(api, "spa", None)
    if spa is None or not api.DIST.exists():
        pytest.skip("frontend bundle not built; static handler is not mounted")
    DIST = api.DIST
    for attempt in ["../../backend/auth.py", "../../data/sample_raw.csv",
                    "../../../../../etc/passwd", "../../requirements.txt"]:
        served = Path(spa(attempt).path).resolve()
        assert served == (DIST / "index.html").resolve(), attempt


def test_oversized_upload_is_rejected(client):
    from backend.main import MAX_UPLOAD_BYTES
    payload = b"Name,Math\n" + b"a,1\n" * (MAX_UPLOAD_BYTES // 4 + 10)
    response = client.post(
        "/api/upload", files={"file": ("huge.csv", payload, "text/csv")}, headers=ADMIN
    )
    assert response.status_code == 413


def test_upload_response_does_not_return_the_entire_log(client):
    from backend.main import MAX_LOG_ENTRIES
    rows = ["Name,Gender,Grade,Math,Science,English,Total"]
    for i in range(3000):
        rows.append(f'"Student{i % 400}",f,Grade {1 + i % 12},{i % 99 + 1} marks,{i % 88 + 1},{i % 77 + 1},0')
    payload = ("\n".join(rows) + "\n").encode()
    response = client.post(
        "/api/upload", files={"file": ("big.csv", payload, "text/csv")}, headers=ADMIN
    )
    report = response.json()["report"]
    assert len(report["entries"]) <= MAX_LOG_ENTRIES
    assert report["entries_total"] > MAX_LOG_ENTRIES
    assert len(response.content) < 1_000_000


def test_student_list_is_capped_but_reports_the_true_count(client):
    response = client.get("/api/students", params={"limit": 50}, headers=ADMIN).json()
    assert response["returned"] <= 50
    assert response["count"] >= response["returned"]
    if response["count"] > 50:
        assert response["truncated"] is True


def test_export_defuses_spreadsheet_formulas(client):
    csv = b'who,=cmd|calc\nAsha,90\nBela,80\nCara,70\n'
    mapping = json.dumps({"name": "who", "subjects": ["=cmd|calc"]})
    client.post("/api/upload", files={"file": ("f.csv", csv, "text/csv")},
                data={"mapping": mapping}, headers=ADMIN)
    header = client.get("/api/export", headers=ADMIN).text.splitlines()[0]
    assert "=cmd" in header and not header.split(",")[3].lstrip('"').startswith("=")

    with REAL.open("rb") as fh:
        client.post("/api/upload", files={"file": ("sample_raw.csv", fh, "text/csv")}, headers=ADMIN)


def test_export_without_credentials_is_refused(client):
    """Regression: the UI used a plain <a href download>, which cannot send
    headers, so the export arrived unauthenticated. The client now fetches
    with the bearer token and saves from a blob."""
    assert client.get("/api/export").status_code == 401
    assert client.get("/api/export/rejects").status_code == 401
    assert client.get("/api/export", headers=STUDENT).status_code == 403
    assert client.get("/api/export", headers=ADMIN).status_code == 200


def test_export_sets_a_content_disposition_filename(client):
    """The blob download reads the filename from this header."""
    response = client.get("/api/export", params={"min_total": 150}, headers=ADMIN)
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition and "filename=" in disposition
    assert "min150" in disposition


def test_statistics_require_a_session(client):
    """Regression: /api/stats was reachable unauthenticated and exposed cohort
    size, score averages, the distribution histogram and debarment counts."""
    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/stats", headers=STUDENT).status_code == 200


def test_bulk_status_rejects_an_oversized_selection(client):
    from backend.main import MAX_BULK_IDS
    response = client.patch(
        "/api/students/status",
        json={"ids": list(range(MAX_BULK_IDS + 1)), "status": "debarred"},
        headers=ADMIN,
    )
    assert response.status_code == 413


def test_security_headers_are_present(client):
    headers = client.get("/api/health").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"

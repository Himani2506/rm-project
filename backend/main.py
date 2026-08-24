"""RM Student Data Pipeline — API and static host.

The built frontend is served from this same application, so the deployed
artefact is a single service on a single origin: no CORS configuration and
WebSockets share the page's scheme and host.
"""

from __future__ import annotations

import io
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pandas as pd
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .auth import (
    WS_TICKET_TTL_SECONDS,
    authenticate,
    current_user,
    issue_token,
    require_admin,
    verify_token,
)
from .cleaning import (
    SUBJECT_SLOTS,
    SchemaError,
    clean_dataframe,
    describe_columns,
    detect_mapping,
    read_upload,
)
from .models import BulkStatusUpdate, LoginRequest, StatusUpdate
from .ws import manager

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "frontend" / "dist"

MAX_UPLOAD_BYTES = 25 * 1024 * 1024   # a cohort file is kilobytes; this is generous
MAX_LOG_ENTRIES = 200                 # full log stays in SQLite; the response is a sample
MAX_ROWS_RETURNED = 1000              # protects the browser from a six-figure payload
MAX_BULK_IDS = 500                    # a bulk action is a UI selection, not a script


def _read_capped(content: bytes) -> None:
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413,
            f"That file is {len(content) / 1e6:.0f} MB. "
            f"The limit is {MAX_UPLOAD_BYTES // 1024 // 1024} MB.",
        )

@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    yield


app = FastAPI(title="RM Student Data Pipeline", version="1.0.0", lifespan=lifespan)

# Cross-origin access is only needed for `vite dev` on :5173. In production the
# frontend is served from this origin, so the middleware is not registered at
# all rather than left permissive.
if os.environ.get("RM_ENV", "development") != "production":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


# --------------------------------------------------------------------------
# api
# --------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "clients": manager.client_count}


@app.post("/api/login")
def login(payload: LoginRequest) -> dict:
    user = authenticate(payload.username, payload.password)
    if user is None:
        # One message for both failure modes, so the response does not reveal
        # which usernames exist.
        raise HTTPException(401, "Incorrect username or password.")
    return {
        "token": issue_token(payload.username.strip().lower(), user["role"]),
        "role": user["role"],
        "label": user["label"],
    }


@app.post("/api/ws-ticket")
def ws_ticket(user: dict = Depends(current_user)) -> dict:
    """Short-lived token for the WebSocket upgrade.

    A browser WebSocket cannot send an Authorization header, so the client
    exchanges its session token for a ticket valid for one minute and passes
    that as a query parameter.
    """
    return {
        "ticket": issue_token(user["sub"], user["role"],
                              ttl=WS_TICKET_TTL_SECONDS, purpose="ws"),
        "expires_in": WS_TICKET_TTL_SECONDS,
    }


@app.post("/api/inspect")
async def inspect(file: UploadFile = File(...), user: dict = Depends(require_admin)) -> dict:
    """Describe an uploaded file without committing it.

    Returns the columns found, a short preview and a suggested role mapping,
    so the admin can confirm or correct it before the data is cleaned.
    """
    content = await file.read()
    _read_capped(content)
    try:
        raw = read_upload(content, file.filename or "upload.csv")
    except SchemaError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not read that file: {exc}") from exc

    suggested = detect_mapping(raw)
    return {
        "filename": file.filename,
        "row_count": len(raw),
        "columns": describe_columns(raw),
        "suggested": suggested,
        "confident": bool(suggested["subjects"]) and suggested["name"] is not None,
        "preview": raw.head(5).fillna("").astype(str).to_dict(orient="records"),
    }


@app.post("/api/upload")
async def upload(
    file: UploadFile = File(...),
    mapping: str | None = Form(default=None),
    user: dict = Depends(require_admin),
) -> dict:
    """Clean and commit a file.

    `mapping` is an optional JSON object of the form
    {"name": col|null, "gender": col|null, "grade": col|null,
     "total": col|null, "subjects": [col, ...]}.
    When omitted the mapping is inferred from the file.
    """
    started = time.perf_counter()
    content = await file.read()
    if not content:
        raise HTTPException(400, "That file is empty. Choose a CSV with a header row and try again.")
    _read_capped(content)

    try:
        raw = read_upload(content, file.filename or "upload.csv")
    except SchemaError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not read that file: {exc}") from exc

    chosen = None
    if mapping:
        try:
            chosen = json.loads(mapping)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "The column mapping was not valid JSON.") from exc

    if chosen is None:
        suggested = detect_mapping(raw)
        if not suggested["subjects"]:
            # Nothing scoreable was found. Rather than rejecting the file,
            # hand the admin everything needed to map the columns by hand.
            return {
                "needs_mapping": True,
                "reason": "No numeric score columns were recognised in this file.",
                "filename": file.filename,
                "row_count": len(raw),
                "columns": describe_columns(raw),
                "suggested": suggested,
                "preview": raw.head(5).fillna("").astype(str).to_dict(orient="records"),
            }
        chosen = suggested

    try:
        cleaned, report = clean_dataframe(raw, mapping=chosen)
    except SchemaError as exc:
        raise HTTPException(422, str(exc)) from exc

    if cleaned.empty:
        raise HTTPException(422, "No usable rows found after cleaning.")

    run_id = db.replace_students(cleaned, report, file.filename or "upload.csv")
    # Every entry is persisted, but returning all of them would mean a
    # multi-megabyte response on a large file. The UI only shows examples.
    summary = report.to_dict()
    summary["entries_total"] = len(summary["entries"])
    summary["entries"] = summary["entries"][:MAX_LOG_ENTRIES]
    payload = {
        "needs_mapping": False,
        "run_id": run_id,
        "report": summary,
        "stats": db.stats(0),
        "total_ms": round((time.perf_counter() - started) * 1000, 2),
    }
    await manager.broadcast({"type": "dataset_replaced", "stats": payload["stats"]})
    return payload


@app.get("/api/students")
def students(
    min_total: int = Query(0, ge=0),
    shortlist_only: bool = False,
    search: str = "",
    limit: int = Query(MAX_ROWS_RETURNED, ge=1, le=MAX_ROWS_RETURNED),
    user: dict = Depends(current_user),
) -> dict:
    rows, elapsed = db.list_students(min_total, shortlist_only, search)
    if user["role"] != "admin":
        # Students see the shortlist, not the administrative flags.
        rows = [
            {k: v for k, v in r.items() if k not in ("quarantine_reason", "source_row", "imputed")}
            for r in rows
            if r["status"] == "active" and not r["quarantined"]
        ]
    matched = len(rows)
    # Exports are unpaginated; the table is not, because rendering six figures
    # of rows helps nobody.
    truncated = matched > limit
    return {
        "students": rows[:limit],
        "query_ms": round(elapsed, 2),
        "count": matched,
        "returned": min(matched, limit),
        "truncated": truncated,
    }


@app.get("/api/stats")
def statistics(min_total: int = Query(0, ge=0), user: dict = Depends(current_user)) -> dict:
    return db.stats(min_total)


@app.get("/api/cleaning-log")
def cleaning_log(user: dict = Depends(require_admin)) -> dict:
    return {"run": db.latest_run(), "entries": db.cleaning_log()}


@app.get("/api/audit")
def audit(user: dict = Depends(require_admin)) -> dict:
    return {"entries": db.audit_trail()}


@app.patch("/api/students/{student_id}/status")
async def update_status(student_id: int, payload: StatusUpdate, user: dict = Depends(require_admin)) -> dict:
    student = db.set_status(student_id, payload.status, user["sub"])
    if student is None:
        raise HTTPException(404, "No student with that id.")
    stats = db.stats(0)
    await manager.broadcast({"type": "status_changed", "student": student, "stats": stats})
    return {"student": student, "stats": stats}


@app.patch("/api/students/status")
async def update_status_bulk(payload: BulkStatusUpdate, user: dict = Depends(require_admin)) -> dict:
    if len(payload.ids) > MAX_BULK_IDS:
        raise HTTPException(
            413, f"Select at most {MAX_BULK_IDS} students in a single action."
        )
    updated = db.set_status_bulk(payload.ids, payload.status, user["sub"])
    stats = db.stats(0)
    await manager.broadcast({"type": "bulk_status_changed", "students": updated, "stats": stats})
    return {"students": updated, "stats": stats}


def _defuse(value):
    """Neutralise spreadsheet formula injection.

    A cell beginning =, +, - or @ is executed as a formula when the exported
    file is opened in Excel or Sheets. Names are already stripped of such
    characters during cleaning, but column labels come straight from the
    uploaded header row, so exports are defused on the way out too.
    """
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def _csv_response(rows: list[dict], columns: list[str], filename: str) -> StreamingResponse:
    """Serialise rows to CSV, renaming the generic score slots to their labels."""
    labels = db.subject_labels()
    rename = {slot: _defuse(label) for slot, label in zip(SUBJECT_SLOTS, labels)}
    headers = [c for c in columns if c not in SUBJECT_SLOTS or c in rename]
    frame = pd.DataFrame(rows)
    if frame.empty:
        frame = pd.DataFrame(columns=headers)
    else:
        frame = frame[[c for c in headers if c in frame.columns]]
    frame = frame.rename(columns=rename)
    for column in frame.columns:
        if frame[column].dtype == object:
            frame[column] = frame[column].map(lambda v: _defuse(v) if isinstance(v, str) else v)
    buffer = io.StringIO()
    frame.to_csv(buffer, index=False)
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/export")
def export(min_total: int = Query(0, ge=0), user: dict = Depends(require_admin)):
    rows, _ = db.list_students(min_total, shortlist_only=True)
    stamp = time.strftime("%Y%m%d-%H%M")
    return _csv_response(
        rows,
        ["name", "gender", "grade", *SUBJECT_SLOTS, "total"],
        f"shortlist-min{min_total}-{stamp}.csv",
    )


@app.get("/api/export/rejects")
def export_rejects(user: dict = Depends(require_admin)):
    rows, _ = db.list_students()
    rejects = [r for r in rows if r["quarantined"]]
    return _csv_response(
        rejects,
        ["source_row", "name", "gender", "grade", *SUBJECT_SLOTS, "quarantine_reason"],
        "quarantined-rows.csv",
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, ticket: str = Query(default="")) -> None:
    """Broadcast subscription, gated by a short-lived signed ticket."""
    try:
        verify_token(ticket, purpose="ws")
    except HTTPException:
        await websocket.close(code=4401)   # application-level "unauthorised"
        return
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # keepalive; clients are not expected to send
    except WebSocketDisconnect:
        await manager.disconnect(websocket)
    except Exception:
        await manager.disconnect(websocket)


# --------------------------------------------------------------------------
# static frontend (mounted last so /api and /ws win)
# --------------------------------------------------------------------------

if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        index = DIST / "index.html"
        if not full_path:
            return FileResponse(index)
        # Resolve and confirm the target is still inside the bundle. Without
        # this, a path such as ../../backend/auth.py escapes the directory and
        # serves application source.
        try:
            candidate = (DIST / full_path).resolve()
            contained = candidate.is_relative_to(DIST.resolve())
        except (OSError, ValueError):
            return FileResponse(index)
        if contained and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)

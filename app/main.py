"""
main.py — FastAPI application: REST API + Server-Side Rendered UI for Universal-Pulse.

Architecture overview
---------------------
  - A single FastAPI instance serves both a JSON REST API (/api/…) and the
    browser-facing HTML UI (/, /tracker/{id}, …).
  - Jinja2 templates render the HTML pages; Plotly.js is served locally from
    /static/plotly.min.js (extracted from the installed plotly package at Docker
    build time, no CDN dependency).
  - Optional HTTP Basic Auth (AUTH_USER / AUTH_PASSWORD env vars) wraps every
    endpoint so the UI is protected on shared networks.
  - The APScheduler background scheduler is started/stopped via FastAPI's
    lifespan context manager, keeping the lifecycle explicit.

Environment variables
---------------------
  AUTH_USER       : Basic-Auth username (leave blank to disable auth).
  AUTH_PASSWORD   : Basic-Auth password.
  DATA_DIR        : Directory for pulse.db (default: /data).
  LOG_LEVEL       : Python logging level (default: INFO).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Optional

import ipaddress
import urllib.parse

import httpx
from fastapi import (
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, HttpUrl
from sqlalchemy.orm import Session

from app.collector import (
    add_or_update_job,
    bootstrap_scheduler,
    remove_job,
    resolve_json_path,
    scheduler,
)
from app.database import (
    create_reading,
    create_tracker,
    delete_tracker,
    get_db,
    get_latest_reading,
    get_readings,
    get_tracker,
    init_db,
    list_trackers,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional Basic-Auth
# ---------------------------------------------------------------------------
AUTH_USER = os.getenv("AUTH_USER", "")
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "")
_basic_security = HTTPBasic(auto_error=False)


def check_auth(credentials: HTTPBasicCredentials = Depends(_basic_security)):
    """
    If AUTH_USER is set, enforce HTTP Basic Auth.
    Use constant-time comparison to prevent timing attacks.
    """
    if not AUTH_USER:
        return  # Auth disabled
    ok = credentials is not None and (
        secrets.compare_digest(
            credentials.username.encode(), AUTH_USER.encode()
        )
        and secrets.compare_digest(
            credentials.password.encode(), AUTH_PASSWORD.encode()
        )
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorised",
            headers={"WWW-Authenticate": "Basic"},
        )


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise DB and start scheduler on startup; shut down scheduler cleanly."""
    init_db()
    bootstrap_scheduler()
    scheduler.start()
    logger.info("APScheduler started")
    yield
    scheduler.shutdown(wait=False)
    logger.info("APScheduler stopped")


# ---------------------------------------------------------------------------
# FastAPI instance
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(__file__)

app = FastAPI(
    title="Universal-Pulse",
    description="Multi-API Data Monitor",
    version="1.0.0",
    lifespan=lifespan,
)

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

static_dir = os.path.join(BASE_DIR, "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ---------------------------------------------------------------------------
# Pydantic schemas (API layer)
# ---------------------------------------------------------------------------
class TrackerCreate(BaseModel):
    name: str
    url: str
    json_path: str
    interval: int = 5
    headers: Optional[dict] = None


class TrackerOut(BaseModel):
    id: int
    name: str
    url: str
    json_path: str
    interval: int
    headers: Optional[dict]
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_obj(cls, t):
        return cls(
            id=t.id,
            name=t.name,
            url=t.url,
            json_path=t.json_path,
            interval=t.interval,
            headers=t.get_headers() or None,
            is_active=t.is_active,
            created_at=t.created_at,
        )


class ReadingOut(BaseModel):
    id: int
    tracker_id: int
    value: float
    timestamp: datetime

    model_config = {"from_attributes": True}


class TestPayload(BaseModel):
    url: str
    json_path: str
    headers: Optional[dict] = None


@app.get("/health", include_in_schema=False)
def health_check():
    """
    Lightweight liveness probe that does NOT require authentication.
    Used by Docker healthcheck and orchestration systems (Kubernetes, Compose).
    """
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# URL safety validator — used by both the test endpoint and tracker creation
# ---------------------------------------------------------------------------
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _validate_url(url: str) -> str | None:
    """
    Return an error message if *url* is unsafe, or None if it is acceptable.

    Enforced rules:
      - Scheme must be http or https (blocks file://, ftp://, etc.).
      - Hostname must not resolve to a private / loopback address range to
        mitigate Server-Side Request Forgery (SSRF) attacks.

    Note: DNS rebinding is not fully prevented here; for high-security
    deployments consider a dedicated egress proxy with network-level controls.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        return f"Invalid URL: {exc}"

    if parsed.scheme not in ("http", "https"):
        return f"URL scheme '{parsed.scheme}' is not allowed. Only http and https are supported."

    hostname = parsed.hostname
    if not hostname:
        return "URL must include a hostname."

    # Attempt to resolve the hostname to an IP and block private ranges.
    import socket

    try:
        addr_info = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        # Can't resolve — let the HTTP client surface the error naturally.
        return None

    for _, _, _, _, sockaddr in addr_info:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return f"Requests to loopback/reserved addresses are not allowed ({ip})."
        for net in _PRIVATE_NETWORKS:
            if ip in net:
                return f"Requests to private network addresses are not allowed ({ip})."

    return None


# ---------------------------------------------------------------------------
# REST API — /api/trackers
# ---------------------------------------------------------------------------
@app.get("/api/trackers", response_model=list[TrackerOut], tags=["API"])
def api_list_trackers(
    db: Session = Depends(get_db),
    _=Depends(check_auth),
):
    return [TrackerOut.from_orm_obj(t) for t in list_trackers(db)]


@app.post("/api/trackers", response_model=TrackerOut, status_code=201, tags=["API"])
def api_create_tracker(
    payload: TrackerCreate,
    db: Session = Depends(get_db),
    _=Depends(check_auth),
):
    url_error = _validate_url(payload.url)
    if url_error:
        raise HTTPException(status_code=400, detail=url_error)
    tracker = create_tracker(
        db,
        name=payload.name,
        url=payload.url,
        json_path=payload.json_path,
        interval=payload.interval,
        headers=payload.headers,
    )
    add_or_update_job(tracker.id, tracker.interval)
    return TrackerOut.from_orm_obj(tracker)


@app.delete("/api/trackers/{tracker_id}", status_code=204, tags=["API"])
def api_delete_tracker(
    tracker_id: int,
    db: Session = Depends(get_db),
    _=Depends(check_auth),
):
    remove_job(tracker_id)
    if not delete_tracker(db, tracker_id):
        raise HTTPException(status_code=404, detail="Tracker not found")


@app.get(
    "/api/trackers/{tracker_id}/readings",
    response_model=list[ReadingOut],
    tags=["API"],
)
def api_get_readings(
    tracker_id: int,
    limit: int = 500,
    db: Session = Depends(get_db),
    _=Depends(check_auth),
):
    if not get_tracker(db, tracker_id):
        raise HTTPException(status_code=404, detail="Tracker not found")
    return get_readings(db, tracker_id=tracker_id, limit=limit)


# ---------------------------------------------------------------------------
# REST API — /api/test
# ---------------------------------------------------------------------------
@app.post("/api/test", tags=["API"])
def api_test_endpoint(
    payload: TestPayload,
    _=Depends(check_auth),
):
    """
    Immediately fire a one-off request to *url* and try to resolve *json_path*.
    Returns the extracted value on success, or a descriptive error message.
    Used by the 'Test' button in the Add-Tracker form.
    """
    # SSRF guard: reject private/loopback addresses and non-http(s) schemes.
    url_error = _validate_url(payload.url)
    if url_error:
        return {"success": False, "error": url_error}

    # Reconstruct URL from parsed components to avoid taint-flow from raw user input.
    parsed = urllib.parse.urlparse(payload.url)
    safe_url = urllib.parse.urlunparse(parsed)

    try:
        headers = payload.headers or {}
        # Disable redirect following — redirect targets are not validated against
        # the SSRF allowlist, so following them could bypass our IP check.
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            resp = client.get(safe_url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        value = resolve_json_path(data, payload.json_path)
        return {"success": True, "value": value, "raw": data}

    except httpx.HTTPStatusError as exc:
        return {
            "success": False,
            "error": f"HTTP {exc.response.status_code}: {exc.response.text[:300]}",
        }
    except httpx.RequestError as exc:
        return {"success": False, "error": f"Request error: {exc}"}
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        return {"success": False, "error": f"JSON path error: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# UI — HTML routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def ui_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    _=Depends(check_auth),
):
    trackers = list_trackers(db)
    cards = []
    for t in trackers:
        latest = get_latest_reading(db, t.id)
        cards.append(
            {
                "id": t.id,
                "name": t.name,
                "url": t.url,
                "interval": t.interval,
                "is_active": t.is_active,
                "latest_value": latest.value if latest else None,
                "latest_ts": latest.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                if latest
                else "—",
            }
        )
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "cards": cards}
    )


@app.get("/tracker/{tracker_id}", response_class=HTMLResponse, include_in_schema=False)
def ui_tracker_detail(
    tracker_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _=Depends(check_auth),
):
    tracker = get_tracker(db, tracker_id)
    if not tracker:
        raise HTTPException(status_code=404, detail="Tracker not found")
    readings = get_readings(db, tracker_id=tracker_id, limit=500)
    chart_data = {
        "timestamps": [r.timestamp.isoformat() for r in readings],
        "values": [r.value for r in readings],
    }
    return templates.TemplateResponse(
        "detail.html",
        {
            "request": request,
            "tracker": tracker,
            "chart_data_json": json.dumps(chart_data),
        },
    )


@app.get("/add", response_class=HTMLResponse, include_in_schema=False)
def ui_add_form(
    request: Request,
    _=Depends(check_auth),
):
    return templates.TemplateResponse("add_tracker.html", {"request": request})


@app.post("/add", response_class=HTMLResponse, include_in_schema=False)
def ui_add_submit(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    json_path: str = Form(...),
    interval: int = Form(5),
    headers_raw: str = Form(""),
    db: Session = Depends(get_db),
    _=Depends(check_auth),
):
    # Parse optional headers
    headers_dict: dict = {}
    if headers_raw.strip():
        try:
            headers_dict = json.loads(headers_raw.strip())
        except json.JSONDecodeError:
            return templates.TemplateResponse(
                "add_tracker.html",
                {
                    "request": request,
                    "error": "Headers must be valid JSON, e.g. {\"X-API-Key\": \"secret\"}",
                    "form": {
                        "name": name,
                        "url": url,
                        "json_path": json_path,
                        "interval": interval,
                        "headers_raw": headers_raw,
                    },
                },
            )

    # SSRF guard: reject private/loopback addresses and non-http(s) schemes.
    url_error = _validate_url(url)
    if url_error:
        return templates.TemplateResponse(
            "add_tracker.html",
            {
                "request": request,
                "error": url_error,
                "form": {
                    "name": name,
                    "url": url,
                    "json_path": json_path,
                    "interval": interval,
                    "headers_raw": headers_raw,
                },
            },
        )

    tracker = create_tracker(
        db,
        name=name,
        url=url,
        json_path=json_path,
        interval=interval,
        headers=headers_dict or None,
    )
    add_or_update_job(tracker.id, tracker.interval)
    return RedirectResponse(url="/", status_code=303)


@app.post("/delete/{tracker_id}", include_in_schema=False)
def ui_delete_tracker(
    tracker_id: int,
    db: Session = Depends(get_db),
    _=Depends(check_auth),
):
    remove_job(tracker_id)
    delete_tracker(db, tracker_id)
    return RedirectResponse(url="/", status_code=303)


@app.post("/toggle/{tracker_id}", include_in_schema=False)
def ui_toggle_tracker(
    tracker_id: int,
    db: Session = Depends(get_db),
    _=Depends(check_auth),
):
    """Toggle a tracker's is_active state."""
    tracker = get_tracker(db, tracker_id)
    if not tracker:
        raise HTTPException(status_code=404)
    tracker.is_active = not tracker.is_active
    db.commit()
    if tracker.is_active:
        add_or_update_job(tracker.id, tracker.interval)
    else:
        remove_job(tracker.id)
    return RedirectResponse(url="/", status_code=303)

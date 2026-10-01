import hashlib
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import telemetry
from .classifier import Classifier, build_classifier
from .config import Settings, load_settings
from .db import init_db
from .routers import catalog, categories, items, sync, sync_schedule
from .routers import firmware as firmware_router
from .schedule import resolve_timezone

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"

# Uvicorn only configures its own loggers, so without this the app's INFO lines (notably which firmware
# version each device reports on sync) would be dropped, leaving only warnings.
_app_log = logging.getLogger("shopping_list")
_STANDARD_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


class _KeyValueFormatter(logging.Formatter):
    """The usual one-line format, plus any structured `extra=` attributes as trailing key=value pairs.

    The OpenTelemetry handler exports those attributes as fields on its own; this is so the same
    detail is visible in the service's plain journal/stdout output too.
    """

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        extras = [f"{k}={v!r}" for k, v in record.__dict__.items() if k not in _STANDARD_RECORD_ATTRS]
        return f"{text} [{' '.join(extras)}]" if extras else text


if not _app_log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(_KeyValueFormatter("%(levelname)s:     %(name)s - %(message)s"))
    _app_log.addHandler(_handler)
    _app_log.setLevel(logging.INFO)

# Paths that are polled or fire per keystroke; their successful requests aren't worth a log line each,
# but a failure still is.
_QUIET_PATH_PREFIXES = ("/api/health", "/api/catalog/suggest", "/static", "/flash")


def _static_version() -> str:
    """Cache-busting query string for the assets index.html links to.

    Derived from their content, so it changes exactly when a deploy changes them. Cache-Control
    alone isn't enough to stop a phone serving a stale app.js: the asset also has to live at a URL
    the browser hasn't already cached.
    """
    h = hashlib.sha256()
    for name in ("app.js", "schedule.js", "style.css"):
        h.update((STATIC_DIR / name).read_bytes())
    return h.hexdigest()[:10]


def create_app(settings: Settings | None = None, classifier: Classifier | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(settings.db_path)
        _app_log.info(
            "server started",
            extra={
                "config.db_path": str(settings.db_path),
                "config.classifier": settings.classifier,
                "config.timezone": str(app.state.tz),
                "config.firmware_target": settings.firmware_version or "none",
                "config.telemetry_enabled": settings.telemetry_enabled,
            },
        )
        yield
        _app_log.info("server stopping")

    app = FastAPI(title="PaperMono Shopping List", lifespan=lifespan)
    app.state.settings = settings
    app.state.classifier = classifier or build_classifier(settings)
    app.state.tz = resolve_timezone(settings.timezone)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    # PaperMono web flasher (esptool-js over WebSerial), folded in here so it's served from the
    # same authenticated origin instead of running as its own separate static site.
    app.mount("/flash", StaticFiles(directory=PACKAGE_DIR / "flasher", html=True), name="flasher")
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    static_version = _static_version()

    @app.middleware("http")
    async def no_heuristic_caching(request: Request, call_next):
        # StaticFiles sends ETag/Last-Modified but no Cache-Control, so browsers fall back to
        # heuristic freshness (RFC 9111 4.2.2) and may skip the request entirely. `no-cache` forces
        # a revalidation, which is still a cheap 304 when nothing changed.
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start = time.perf_counter()
        attrs = {"http.method": request.method, "http.target": request.url.path}
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000)
            _app_log.exception(
                "unhandled error on %s %s",
                request.method,
                request.url.path,
                extra={**attrs, "http.duration_ms": duration_ms},
            )
            raise
        status = response.status_code
        if status >= 400 or not request.url.path.startswith(_QUIET_PATH_PREFIXES):
            level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
            _app_log.log(
                level,
                "%s %s -> %d",
                request.method,
                request.url.path,
                status,
                extra={
                    **attrs,
                    "http.status_code": status,
                    "http.duration_ms": round((time.perf_counter() - start) * 1000),
                    "client.address": request.client.host if request.client else None,
                },
            )
        return response

    if settings.telemetry_enabled:
        # Observability must never keep the list itself from starting (e.g. the `otel` extra missing).
        try:
            telemetry.setup(app)
        except Exception:
            logging.getLogger(__name__).exception("OpenTelemetry setup failed; continuing without it")

    app.include_router(categories.router)
    app.include_router(items.router)
    app.include_router(catalog.router)
    app.include_router(sync.router)
    app.include_router(sync_schedule.router)
    app.include_router(firmware_router.router)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(request: Request):
        return templates.TemplateResponse(request, "index.html", {"static_version": static_version})

    @app.get("/schedule", response_class=HTMLResponse, include_in_schema=False)
    def schedule_page(request: Request):
        return templates.TemplateResponse(request, "schedule.html", {"static_version": static_version})

    @app.get("/api/health", tags=["health"])
    def health():
        return {"status": "ok"}

    return app


app = create_app()

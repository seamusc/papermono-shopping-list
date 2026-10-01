import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .. import firmware, telemetry

router = APIRouter(prefix="/api", tags=["firmware"])
log = logging.getLogger(__name__)


@router.get("/firmware/{version}.bin", response_class=FileResponse)
def download(version: str, request: Request):
    """The app-only OTA image for `version`. FileResponse supplies Content-Length, ETag and Range."""
    path = firmware.image_path(request.app.state.settings, version)
    if path is None:
        log.warning("firmware download for unknown version %r", version, extra={"firmware.version": version})
        raise HTTPException(status_code=404, detail="no such firmware version")
    # Range requests (resumed downloads) re-hit this route too; count only the first byte-range.
    if not request.headers.get("range", "").startswith("bytes=") or request.headers["range"].startswith(
        "bytes=0-"
    ):
        telemetry.firmware_downloads.add(1, {"version": version})
        log.info(
            "firmware %s download started",
            version,
            extra={"firmware.version": version, "firmware.size": path.stat().st_size},
        )
    return FileResponse(path, media_type="application/octet-stream", filename=f"{version}.bin")

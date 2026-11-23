import asyncio
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response

from app.config import settings
from app.face_engine import FaceEngine, ImageRejected
from app.jobs import ScanError
from app.live import LiveHub, LiveSession, run_live
from app.live_source import empty_note, parse_source
from app.paths import data_root
from app.schemas import LiveResponse

router = APIRouter()


def _hub(request: Request) -> LiveHub:
    return request.app.state.live


def _present(session: LiveSession) -> LiveResponse:
    return LiveResponse(**session.view())


def _launch(request: Request, session: LiveSession, source, work_dir) -> None:
    lock = request.app.state.scan_lock
    engine: FaceEngine = request.app.state.engine
    plates = request.app.state.plates

    async def runner() -> None:
        if _hub(request).get(session.id) is None or session.stopped():
            session.mark_stopped()
            return
        if lock.locked():
            session.set_message("Waiting for the other scan to finish")
        async with lock:
            if _hub(request).get(session.id) is None or session.stopped():
                session.mark_stopped()
                return
            await asyncio.to_thread(run_live, session, source, work_dir, engine, plates)

    session.task = asyncio.create_task(runner())


@router.post("/api/live", response_model=LiveResponse, tags=["live"])
async def start_live(
    request: Request,
    source: str = Form(...),
    watch: str = Form("both"),
    plate: str = Form(""),
    file: UploadFile | None = File(None),
    min_similarity: float | None = Query(None, ge=0.0, le=1.0),
    sample_fps: float | None = Query(None, ge=0.5, le=5),
) -> LiveResponse:
    """Watch a camera or a stream for one face, for plates, or for both."""
    if watch not in {"faces", "plates", "both"}:
        raise HTTPException(status_code=400, detail="Choose faces, plates, or both.")
    plate_text = plate.strip()
    if len(plate_text) > 16:
        raise HTTPException(status_code=400, detail="Enter a plate of at most 16 characters.")
    try:
        parsed = parse_source(source)
    except ScanError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    embedding = None
    if watch in {"faces", "both"}:
        if file is None or not file.filename:
            raise HTTPException(status_code=400, detail="Choose a face to look for.")
        from app.main import _read_upload

        data = await _read_upload(file)
        engine: FaceEngine = request.app.state.engine
        try:
            detected = await asyncio.to_thread(engine.describe, data)
        except ImageRejected as exc:
            status = 422 if exc.reason in {"no_face", "too_small"} else 400
            raise HTTPException(status_code=status, detail=exc.message) from exc
        embedding = detected.embedding

    from app.main import _floor

    session = LiveSession(
        source_label=parsed.label,
        watch=watch,
        sample_fps=sample_fps if sample_fps is not None else settings.video_sample_fps,
        plate_query=plate_text,
        min_similarity=_floor(min_similarity),
        empty_note=empty_note(watch, plate_text),
    )
    session.embedding = embedding
    work_dir = data_root() / "jobs" / str(session.id)
    work_dir.mkdir(parents=True, exist_ok=True)
    _hub(request).install(session)
    _launch(request, session, parsed, work_dir)
    return _present(session)


@router.get("/api/live/current", response_model=LiveResponse, tags=["live"])
async def current_live(request: Request) -> LiveResponse:
    session = _hub(request).current()
    if session is None:
        raise HTTPException(status_code=404, detail="No camera is open.")
    return _present(session)


@router.get("/api/live/{session_id}", response_model=LiveResponse, tags=["live"])
async def live_status(session_id: UUID, request: Request) -> LiveResponse:
    session = _hub(request).get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="That camera watch is no longer available. Start it again.")
    return _present(session)


@router.post("/api/live/{session_id}/stop", response_model=LiveResponse, tags=["live"])
async def stop_live(session_id: UUID, request: Request) -> LiveResponse:
    session = _hub(request).get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="That camera watch is no longer available. Start it again.")
    session.request_stop()
    session.mark_stopped()
    return _present(session)


@router.get("/api/live/{session_id}/preview", tags=["live"])
async def live_preview(session_id: UUID, request: Request) -> Response:
    session = _hub(request).get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="That camera watch is no longer available. Start it again.")
    preview = session.preview_bytes()
    if not preview:
        raise HTTPException(status_code=404, detail="No picture yet.")
    return Response(content=preview, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

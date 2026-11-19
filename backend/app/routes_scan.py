import asyncio
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.config import settings
from app.face_engine import FaceEngine, ImageRejected
from app.gallery_build import replace_from_folder
from app.jobs import Job, finish
from app.paths import IMAGE_EXT, VIDEO_EXT, PathError, data_root, list_folder, resolve_inside, safe_parts
from app.scan_folder import index_and_search
from app.scan_video import scan_face_video, scan_plate_video
from app.schemas import FolderListing, FolderUploadResponse, JobResponse

router = APIRouter()

MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


def _present(job: Job) -> JobResponse:
    return JobResponse(
        job_id=job.id,
        kind=job.kind,
        status=job.status,
        progress=round(job.progress, 4),
        message=job.message,
        error=job.error,
        folder=job.folder,
        scan=job.scan,
        gallery=job.gallery,
    )


def _launch(request: Request, job: Job, work) -> None:
    lock = request.app.state.scan_lock

    async def runner() -> None:
        if lock.locked():
            job.message = "Waiting for the other scan to finish"
        async with lock:
            await finish(job, work)

    job.task = asyncio.create_task(runner())


def _sample_fps(value: float | None) -> float:
    if value is None:
        return settings.video_sample_fps
    return value


async def _transcode_preview(source: Path, dest: Path) -> None:
    """Make a browser-playable H.264 copy. Chrome cannot decode AVI."""
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-an",
        "-vf",
        "scale='min(960,iw)':-2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(dest),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await process.communicate()
    if process.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        tail = err.decode("utf-8", "replace").strip().splitlines()
        detail = tail[-1] if tail else "ffmpeg failed"
        raise HTTPException(status_code=422, detail=f"This video could not be prepared for preview. {detail}")


async def _save_stream(upload: UploadFile, dest: Path, limit: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with dest.open("wb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise HTTPException(status_code=413, detail="That file is too large.")
                handle.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="The upload was empty.")
    except Exception:
        dest.unlink(missing_ok=True)
        raise


@router.get("/api/folders", response_model=FolderListing, tags=["folders"])
async def folders(path: str = Query("")) -> FolderListing:
    """List folders under the data directory and count images inside the chosen one."""
    try:
        return FolderListing(**list_folder(path))
    except PathError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc


@router.post("/api/folders/upload", response_model=FolderUploadResponse, tags=["folders"])
async def upload_folder(files: list[UploadFile] = File(...)) -> FolderUploadResponse:
    """Store a browser-selected folder under data/sources and return its path."""
    if not files:
        raise HTTPException(status_code=400, detail="Choose a folder that contains images.")
    token = uuid.uuid4().hex
    destination = data_root() / "sources" / token
    destination.mkdir(parents=True, exist_ok=True)
    saved = 0
    total = 0
    tops: set[str] = set()
    try:
        for upload in files:
            parts = safe_parts(upload.filename or "")
            if not parts:
                continue
            if Path(parts[-1]).suffix.lower() not in IMAGE_EXT:
                continue
            if saved >= settings.max_folder_files:
                raise HTTPException(
                    status_code=413,
                    detail=f"Upload at most {settings.max_folder_files:,} images.",
                )
            target = destination.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            await _save_stream(upload, target, settings.max_folder_image_bytes)
            total += target.stat().st_size
            if total > settings.max_video_bytes:
                raise HTTPException(status_code=413, detail="That folder is larger than 2 GB.")
            saved += 1
            tops.add(parts[0])
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    if saved == 0:
        shutil.rmtree(destination, ignore_errors=True)
        raise HTTPException(status_code=400, detail="That folder has no images.")
    relative = f"sources/{token}"
    if len(tops) == 1:
        relative = f"{relative}/{next(iter(tops))}"
    return FolderUploadResponse(path=relative, image_count=saved)


@router.post("/api/gallery/replace", response_model=JobResponse, tags=["gallery"])
async def replace_gallery(request: Request, folder: str = Query(..., min_length=1)) -> JobResponse:
    """Delete the enrolled gallery and enroll faces from a folder under data."""
    job = request.app.state.jobs.create("gallery-replace")
    pool = request.app.state.pool
    engine: FaceEngine = request.app.state.engine

    async def work(current: Job) -> None:
        result = await replace_from_folder(pool, engine, folder, current.touch)
        current.gallery = result
        current.message = (
            f"Gallery replaced. {result['enrolled']} faces enrolled, "
            f"{result['skipped']} photos skipped."
        )

    _launch(request, job, work)
    return _present(job)


@router.post("/api/folder-search", response_model=JobResponse, tags=["folders"])
async def folder_search(
    request: Request,
    file: UploadFile = File(...),
    folder: str = Form(...),
    min_similarity: float | None = Query(None, ge=0.0, le=1.0),
) -> JobResponse:
    """Index a data-directory folder if needed, then compare one face with it."""
    from app.main import _floor, _read_upload

    data = await _read_upload(file)
    engine: FaceEngine = request.app.state.engine
    try:
        detected = await asyncio.to_thread(engine.describe, data)
    except ImageRejected as exc:
        status = 422 if exc.reason in {"no_face", "too_small"} else 400
        raise HTTPException(status_code=status, detail=exc.message) from exc
    floor = _floor(min_similarity)
    face_box = [round(value, 5) for value in detected.box]
    job = request.app.state.jobs.create("folder")

    async def work(current: Job) -> None:
        result = await index_and_search(
            pool=request.app.state.pool,
            engine=engine,
            folder_relative=folder,
            embedding=detected.embedding,
            face_box=face_box,
            min_similarity=floor,
            progress=current.touch,
        )
        current.folder = result
        label = "match" if result["total"] == 1 else "matches"
        current.message = f"{result['total']} {label}"

    _launch(request, job, work)
    return _present(job)


@router.post("/api/video/preview", tags=["video"])
async def video_preview(video: UploadFile = File(...)) -> FileResponse:
    """Return an H.264 MP4 the browser can play. Used for AVI and other unsupported files."""
    ext = Path(video.filename or "").suffix.lower()
    if ext not in VIDEO_EXT:
        raise HTTPException(
            status_code=415,
            detail="Upload a video file (mp4, mov, avi, mkv, or webm).",
        )
    work = data_root() / "jobs" / "previews" / uuid.uuid4().hex
    work.mkdir(parents=True, exist_ok=True)
    source = work / f"source{ext}"
    dest = work / "preview.mp4"
    try:
        await _save_stream(video, source, settings.max_video_bytes)
        await _transcode_preview(source, dest)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return FileResponse(
        dest,
        media_type="video/mp4",
        filename="preview.mp4",
        background=BackgroundTask(shutil.rmtree, work, True),
    )


@router.post("/api/video/face", response_model=JobResponse, tags=["video"])
async def video_face(
    request: Request,
    file: UploadFile = File(...),
    video: UploadFile = File(...),
    min_similarity: float | None = Query(None, ge=0.0, le=1.0),
    sample_fps: float | None = Query(None, ge=0.5, le=5),
) -> JobResponse:
    """Search a video for one face and return the time ranges where it appears."""
    from app.main import _floor, _read_upload

    data = await _read_upload(file)
    engine: FaceEngine = request.app.state.engine
    try:
        detected = await asyncio.to_thread(engine.describe, data)
    except ImageRejected as exc:
        status = 422 if exc.reason in {"no_face", "too_small"} else 400
        raise HTTPException(status_code=status, detail=exc.message) from exc
    job = request.app.state.jobs.create("video-face")
    work_dir = data_root() / "jobs" / str(job.id)
    work_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(video.filename or "").suffix.lower()
    if ext not in VIDEO_EXT:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(
            status_code=415,
            detail="Upload a video file (mp4, mov, avi, mkv, or webm).",
        )
    video_path = work_dir / f"source{ext}"
    try:
        await _save_stream(video, video_path, settings.max_video_bytes)
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    floor = _floor(min_similarity)
    fps = _sample_fps(sample_fps)

    async def work(current: Job) -> None:
        result = await asyncio.to_thread(
            scan_face_video,
            engine=engine,
            video_path=video_path,
            work_dir=work_dir,
            embedding=detected.embedding,
            min_similarity=floor,
            sample_fps=fps,
            progress=current.touch,
        )
        current.scan = result
        count = len(result["hits"])
        label = "time range" if count == 1 else "time ranges"
        current.message = f"{count} {label}" if count else result["note"]

    _launch(request, job, work)
    return _present(job)


@router.post("/api/video/plates", response_model=JobResponse, tags=["video"])
async def video_plates(
    request: Request,
    video: UploadFile = File(...),
    plate: str = Form(""),
    sample_fps: float | None = Query(None, ge=0.5, le=5),
) -> JobResponse:
    """Read license plates in a video and return the time range for each one."""
    job = request.app.state.jobs.create("video-plates")
    work_dir = data_root() / "jobs" / str(job.id)
    work_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(video.filename or "").suffix.lower()
    if ext not in VIDEO_EXT:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(
            status_code=415,
            detail="Upload a video file (mp4, mov, avi, mkv, or webm).",
        )
    if len(plate.strip()) > 16:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="Enter a plate of at most 16 characters.")
    video_path = work_dir / f"source{ext}"
    try:
        await _save_stream(video, video_path, settings.max_video_bytes)
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    fps = _sample_fps(sample_fps)
    plates = request.app.state.plates

    async def work(current: Job) -> None:
        result = await asyncio.to_thread(
            scan_plate_video,
            plates=plates,
            video_path=video_path,
            work_dir=work_dir,
            plate_query=plate.strip(),
            sample_fps=fps,
            progress=current.touch,
        )
        current.scan = result
        count = len(result["hits"])
        label = "plate" if count == 1 else "plates"
        current.message = f"{count} {label}" if count else result["note"]

    _launch(request, job, work)
    return _present(job)


@router.get("/api/jobs/{job_id}", response_model=JobResponse, tags=["video"])
async def job_status(job_id: str, request: Request) -> JobResponse:
    job = request.app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="That scan is no longer available. Start it again.")
    return _present(job)


@router.get("/api/media", tags=["folders"])
async def media(path: str = Query(...)) -> FileResponse:
    """Serve an image from the data directory. Used for folder matches and video snapshots."""
    try:
        file_path = resolve_inside(path, must_exist=True)
    except PathError as exc:
        raise HTTPException(status_code=404, detail="Image not found.") from exc
    if not file_path.is_file() or file_path.suffix.lower() not in IMAGE_EXT:
        raise HTTPException(status_code=404, detail="Image not found.")
    return FileResponse(
        file_path,
        media_type=MEDIA_TYPES.get(file_path.suffix.lower(), "application/octet-stream"),
        headers={"Cache-Control": "private, max-age=3600"},
    )

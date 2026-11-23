import asyncio
import math
import re
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.config import settings
from app.db import (
    count_faces,
    create_search,
    get_search,
    open_pool,
    page_matches,
    sample_faces,
)
from app.face_engine import FaceEngine, ImageRejected
from app.jobs import JobStore
from app.live import LiveHub
from app.log import configure_logging
from app.paths import ensure_data_dirs, purge_old_jobs
from app.plate_engine import PlateEngine
from app.routes_live import router as live_router
from app.routes_scan import router as scan_router
from app.schemas import (
    HealthResponse,
    MatchResponse,
    SampleResponse,
    SearchResponse,
    StatsResponse,
)

configure_logging()

SHA1 = re.compile(r"^[0-9a-f]{40}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_data_dirs()
    purge_old_jobs()
    app.state.pool = await open_pool()
    app.state.engine = FaceEngine()
    app.state.jobs = JobStore()
    app.state.plates = PlateEngine()
    app.state.scan_lock = asyncio.Lock()
    app.state.live = LiveHub()
    yield
    await app.state.live.shutdown()
    await app.state.pool.close()


app = FastAPI(
    title="Falcon Eye Search Engine",
    version="1.0.0",
    summary="Match one portrait against a gallery, a folder, a CCTV video, or a live camera.",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
    lifespan=lifespan,
)

app.include_router(scan_router)
app.include_router(live_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origin_list,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _floor(min_similarity: float | None) -> float:
    if min_similarity is None:
        return settings.default_min_similarity
    return min_similarity


def _match(row) -> MatchResponse:
    similarity = max(0.0, min(1.0, float(row["similarity"])))
    qid = row["wikidata_id"]
    return MatchResponse(
        person_name=row["person_name"],
        file_sha1=row["file_sha1"],
        similarity=round(similarity, 6),
        match_percent=round(similarity * 100, 1),
        photo_url=f"/api/photos/{row['file_sha1']}",
        source_url=row["source_url"],
        author=row["author"],
        license=row["license"],
        license_url=row["license_url"],
        wikidata_url=f"https://www.wikidata.org/wiki/{qid}" if qid else None,
    )


def _response(
    *,
    search_id: UUID,
    page: int,
    page_size: int,
    total: int,
    min_similarity: float,
    faces_found: int,
    face_box: list[float] | None,
    rows,
) -> SearchResponse:
    pages = math.ceil(total / page_size) if total else 0
    return SearchResponse(
        search_id=search_id,
        page=page,
        page_size=page_size,
        total=total,
        pages=pages,
        has_previous=page > 1 and total > 0,
        has_next=page * page_size < total,
        min_similarity=min_similarity,
        faces_found=faces_found,
        face_box=face_box,
        results=[_match(row) for row in rows],
    )


async def _read_upload(file: UploadFile) -> bytes:
    if file.content_type and not (
        file.content_type.startswith("image/")
        or file.content_type == "application/octet-stream"
    ):
        raise HTTPException(status_code=415, detail="Upload an image file.")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="Photo is larger than 10 MB.")
        chunks.append(chunk)
    if not chunks:
        raise HTTPException(status_code=400, detail="The upload was empty.")
    return b"".join(chunks)


@app.get("/api/health", response_model=HealthResponse, tags=["meta"])
async def health(request: Request) -> HealthResponse:
    try:
        enrolled = await count_faces(request.app.state.pool)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database is unavailable.") from exc
    return HealthResponse(
        status="ok",
        enrolled_faces=enrolled,
        model_name=settings.model_name,
    )


@app.get("/api/stats", response_model=StatsResponse, tags=["meta"])
async def stats(request: Request) -> StatsResponse:
    enrolled = await count_faces(request.app.state.pool)
    return StatsResponse(
        enrolled_faces=enrolled,
        model_name=settings.model_name,
        embedding_dim=settings.embedding_dim,
        det_size=settings.det_size,
    )


@app.get("/api/samples", response_model=list[SampleResponse], tags=["meta"])
async def samples(request: Request, limit: int = Query(8, ge=1, le=24)) -> list[SampleResponse]:
    rows = await sample_faces(request.app.state.pool, limit)
    return [
        SampleResponse(
            person_name=row["person_name"],
            file_sha1=row["file_sha1"],
            photo_url=f"/api/photos/{row['file_sha1']}",
        )
        for row in rows
    ]


@app.post("/api/search", response_model=SearchResponse, tags=["search"])
async def search(
    request: Request,
    file: UploadFile = File(...),
    page: int = Query(1, ge=1, le=100000),
    page_size: int = Query(12, ge=1, le=60),
    min_similarity: float | None = Query(None, ge=0.0, le=1.0),
) -> SearchResponse:
    """Accept one photo, embed its largest face, and return one page of similar gallery photos."""
    data = await _read_upload(file)
    engine: FaceEngine = request.app.state.engine
    try:
        detected = await asyncio.to_thread(engine.describe, data)
    except ImageRejected as exc:
        status = 422 if exc.reason in {"no_face", "too_small"} else 400
        raise HTTPException(status_code=status, detail=exc.message) from exc
    floor = _floor(min_similarity)
    search_id = await create_search(
        request.app.state.pool,
        detected.embedding,
        detected.faces_found,
        detected.box,
    )
    total, rows = await page_matches(
        request.app.state.pool,
        detected.embedding,
        floor,
        page,
        page_size,
    )
    return _response(
        search_id=search_id,
        page=page,
        page_size=page_size,
        total=total,
        min_similarity=floor,
        faces_found=detected.faces_found,
        face_box=[round(value, 5) for value in detected.box],
        rows=rows,
    )


@app.get("/api/search/{search_id}", response_model=SearchResponse, tags=["search"])
async def search_page(
    search_id: UUID,
    request: Request,
    page: int = Query(1, ge=1, le=100000),
    page_size: int = Query(12, ge=1, le=60),
    min_similarity: float | None = Query(None, ge=0.0, le=1.0),
) -> SearchResponse:
    """Page through a search without uploading the photo again."""
    stored = await get_search(request.app.state.pool, search_id)
    if stored is None:
        raise HTTPException(
            status_code=404,
            detail="This search expired. Upload the photo again.",
        )
    floor = _floor(min_similarity)
    embedding = stored["embedding"]
    total, rows = await page_matches(
        request.app.state.pool,
        embedding,
        floor,
        page,
        page_size,
    )
    box = [stored["box_x1"], stored["box_y1"], stored["box_x2"], stored["box_y2"]]
    face_box = None if any(value is None for value in box) else [round(float(value), 5) for value in box]
    return _response(
        search_id=search_id,
        page=page,
        page_size=page_size,
        total=total,
        min_similarity=floor,
        faces_found=int(stored["faces_found"]),
        face_box=face_box,
        rows=rows,
    )


@app.get("/api/photos/{file_sha1}", tags=["photos"])
async def photo(file_sha1: str) -> FileResponse:
    """Serve a gallery JPEG by the SHA-1 stored in the database."""
    if SHA1.fullmatch(file_sha1) is None:
        raise HTTPException(status_code=404, detail="Photo not found.")
    root = Path(settings.photo_dir).resolve()
    path = (root / f"{file_sha1}.jpg").resolve()
    if path.parent != root or not path.is_file():
        raise HTTPException(status_code=404, detail="Photo not found.")
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )

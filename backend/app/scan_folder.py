import asyncio
import logging
import re
from urllib.parse import quote

from app.config import settings
from app.db import (
    count_indexed_folder_files,
    drop_missing_folder_files,
    folder_file_meta,
    gallery_embeddings,
    replace_folder_file,
    search_folder_faces,
)
from app.face_engine import FaceEngine, ImageRejected
from app.jobs import ScanError
from app.paths import PathError, iter_images, resolve_inside

logger = logging.getLogger(__name__)

SHA1 = re.compile(r"^[0-9a-f]{40}$")
RESULT_LIMIT = 120


def _file_url(folder_relative: str, relative_path: str) -> str:
    prefix = folder_relative.strip("/")
    full = f"{prefix}/{relative_path}" if prefix else relative_path
    return f"/api/media?path={quote(full)}"


async def index_and_search(
    *,
    pool,
    engine: FaceEngine,
    folder_relative: str,
    embedding,
    face_box: list[float] | None,
    min_similarity: float,
    progress,
) -> dict:
    try:
        folder = resolve_inside(folder_relative, must_exist=True)
    except PathError as exc:
        raise ScanError(exc.message) from exc
    if not folder.is_dir():
        raise ScanError("That path is not a folder.")
    images = list(iter_images(folder))
    if not images:
        raise ScanError("That folder has no images.")
    if len(images) > settings.max_folder_files:
        raise ScanError(
            f"That folder has more than {settings.max_folder_files:,} images. Choose a smaller one."
        )

    root_key = str(folder)
    known = await folder_file_meta(pool, root_key)
    pending = []
    current_paths: list[str] = []
    for path in images:
        relative = path.relative_to(folder).as_posix()
        current_paths.append(relative)
        try:
            stat = path.stat()
        except OSError:
            continue
        cached = known.get(relative)
        if cached and cached == (int(stat.st_mtime_ns), int(stat.st_size)):
            continue
        pending.append((path, relative, int(stat.st_mtime_ns), int(stat.st_size)))

    progress(0.02, f"Indexing {len(pending)} new images of {len(images)}")
    reused = 0
    embedded = 0
    skipped = 0
    sha_needed: list[tuple] = []
    detect_needed: list[tuple] = []
    for item in pending:
        stem = item[0].stem.lower()
        if SHA1.fullmatch(stem):
            sha_needed.append((*item, stem))
        else:
            detect_needed.append(item)

    gallery = await gallery_embeddings(pool, [item[-1] for item in sha_needed])
    for path, relative, mtime_ns, size_bytes, stem in sha_needed:
        existing = gallery.get(stem)
        if existing is None:
            detect_needed.append((path, relative, mtime_ns, size_bytes))
            continue
        await replace_folder_file(
            pool,
            root_path=root_key,
            relative_path=relative,
            mtime_ns=mtime_ns,
            size_bytes=size_bytes,
            embeddings=[existing],
            skip_reason=None,
        )
        reused += 1

    total_pending = max(1, len(detect_needed) + reused)
    done = reused
    for path, relative, mtime_ns, size_bytes in detect_needed:
        done += 1
        if done == 1 or done % 10 == 0 or done == len(detect_needed) + reused:
            progress(min(0.9, done / total_pending), f"Reading faces {done} of {len(pending) or len(images)}")
        embeddings, reason = await asyncio.to_thread(_embed_file, engine, path, size_bytes)
        if reason:
            skipped += 1
        else:
            embedded += 1
        await replace_folder_file(
            pool,
            root_path=root_key,
            relative_path=relative,
            mtime_ns=mtime_ns,
            size_bytes=size_bytes,
            embeddings=embeddings,
            skip_reason=reason,
        )

    await drop_missing_folder_files(pool, root_key, current_paths)
    progress(0.95, "Comparing the selected face")
    total, rows = await search_folder_faces(
        pool,
        root_key,
        embedding,
        min_similarity,
        RESULT_LIMIT,
    )
    folder_label = folder_relative.strip("/") or "data"
    results = [
        {
            "name": row["relative_path"].rsplit("/", 1)[-1],
            "relative_path": row["relative_path"],
            "photo_url": _file_url(folder_label if folder_label != "data" else "", row["relative_path"]),
            "similarity": round(max(0.0, min(1.0, float(row["similarity"]))), 6),
            "match_percent": round(max(0.0, min(1.0, float(row["similarity"]))) * 100, 1),
        }
        for row in rows
    ]
    with_faces = await count_indexed_folder_files(pool, root_key)
    logger.info(
        "folder search %s indexed reused=%s embedded=%s skipped=%s matches=%s",
        folder_label,
        reused,
        embedded,
        skipped,
        total,
    )
    return {
        "folder": folder_label,
        "examined": len(images),
        "with_faces": with_faces,
        "skipped": skipped,
        "face_box": face_box,
        "total": total,
        "results": results,
    }


def _embed_file(engine: FaceEngine, path, size_bytes: int):
    if size_bytes > settings.max_folder_image_bytes:
        return [], "too_large"
    try:
        data = path.read_bytes()
    except OSError:
        return [], "unreadable"
    try:
        faces = engine.embed_bytes(data)
    except ImageRejected:
        return [], "no_face"
    except Exception:
        logger.exception("could not read %s", path)
        return [], "unreadable"
    if not faces:
        return [], "no_face"
    return [face.embedding for face in faces], None

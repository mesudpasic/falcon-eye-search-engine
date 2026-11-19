import asyncio
import hashlib
import io
import logging
import shutil
import uuid
from pathlib import Path

from PIL import Image, ImageOps

from app.config import settings
from app.db import insert_face, reset_gallery
from app.face_engine import FaceEngine, ImageRejected
from app.jobs import ScanError
from app.paths import data_root, iter_images, resolve_inside

logger = logging.getLogger(__name__)


def _person_name(filename: str) -> str:
    stem = Path(filename).stem.replace("_", " ")
    stem = " ".join(stem.split())
    return (stem or "Photo")[:180]


def _to_jpeg(data: bytes) -> tuple[bytes, int, int] | None:
    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        image = image.convert("RGB")
    except Exception:
        return None
    width, height = image.size
    edge = settings.max_image_edge
    if max(width, height) > edge:
        scale = edge / max(width, height)
        image = image.resize(
            (max(1, round(width * scale)), max(1, round(height * scale))),
            Image.Resampling.LANCZOS,
        )
        width, height = image.size
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=86)
    return buffer.getvalue(), width, height


def _source_images(folder: Path) -> list[Path]:
    photo_root = Path(settings.photo_dir).resolve()
    chosen = folder.resolve()
    reading_store = chosen == photo_root or photo_root in chosen.parents
    images = []
    for path in iter_images(folder):
        resolved = path.resolve()
        if not reading_store and (resolved == photo_root or photo_root in resolved.parents):
            continue
        images.append(path)
    return images


def _clear_photo_files() -> None:
    folder = Path(settings.photo_dir)
    folder.mkdir(parents=True, exist_ok=True)
    for path in folder.glob("*.jpg"):
        path.unlink(missing_ok=True)
    for path in folder.glob("*.jpg.tmp"):
        path.unlink(missing_ok=True)


def _prepare_one(engine: FaceEngine, path: Path) -> dict | None:
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    converted = _to_jpeg(payload)
    if converted is None:
        return None
    jpeg, width, height = converted
    try:
        detected = engine.describe(jpeg)
    except ImageRejected:
        return None
    digest = hashlib.sha1(jpeg).hexdigest()
    return {
        "name": _person_name(path.name),
        "sha1": digest,
        "jpeg": jpeg,
        "width": width,
        "height": height,
        "embedding": detected.embedding,
    }


async def replace_from_folder(pool, engine: FaceEngine, folder_relative: str, progress) -> dict:
    """Build a new gallery from a folder. The current gallery is deleted only after a face is found."""
    try:
        folder = resolve_inside(folder_relative, must_exist=True)
    except Exception as exc:
        message = getattr(exc, "message", None) or "That folder was not found."
        raise ScanError(message) from exc
    if not folder.is_dir():
        raise ScanError("That path is not a folder.")
    chosen = folder.resolve()
    photo_root = Path(settings.photo_dir).resolve()
    if chosen == data_root():
        raise ScanError("Choose a folder inside data, not the data root.")
    if chosen == photo_root or photo_root in chosen.parents:
        raise ScanError("Choose a folder of new photos, not the enrolled gallery store.")

    images = _source_images(folder)
    if not images:
        raise ScanError("That folder has no images. The gallery was not changed.")
    if len(images) > settings.max_folder_files:
        raise ScanError(
            f"That folder has more than {settings.max_folder_files:,} images. Choose a smaller one."
        )

    stage = data_root() / "jobs" / "gallery-build" / uuid.uuid4().hex
    ready = stage / "ready"
    ready.mkdir(parents=True, exist_ok=True)
    prepared: list[dict] = []
    skipped = 0
    try:
        progress(0.02, f"Reading {len(images)} photos")
        for index, path in enumerate(images, start=1):
            if index == 1 or index % 5 == 0 or index == len(images):
                progress(
                    min(0.82, index / len(images) * 0.8),
                    f"Reading faces {index} of {len(images)}",
                )
            record = await asyncio.to_thread(_prepare_one, engine, path)
            if record is None:
                skipped += 1
                continue
            target = ready / f"{record['sha1']}.jpg"
            if not target.exists():
                target.write_bytes(record["jpeg"])
            record.pop("jpeg")
            prepared.append(record)

        if not prepared:
            raise ScanError("No face was found in that folder. The gallery was not changed.")

        progress(0.86, "Replacing the gallery")
        await reset_gallery(pool)
        await asyncio.to_thread(_clear_photo_files)
        photo_dir = Path(settings.photo_dir)
        enrolled = 0
        seen: set[str] = set()
        for index, record in enumerate(prepared, start=1):
            if record["sha1"] in seen:
                skipped += 1
                continue
            seen.add(record["sha1"])
            source = ready / f"{record['sha1']}.jpg"
            destination = photo_dir / f"{record['sha1']}.jpg"
            shutil.move(str(source), destination)
            inserted = await insert_face(
                pool,
                file_sha1=record["sha1"],
                person_name=record["name"],
                wikidata_id=None,
                embedding=record["embedding"],
                source_url=None,
                author=None,
                license_name=None,
                license_url=None,
                width=record["width"],
                height=record["height"],
            )
            if inserted:
                enrolled += 1
            else:
                skipped += 1
            if index == 1 or index % 20 == 0 or index == len(prepared):
                progress(0.86 + 0.13 * index / len(prepared), f"Saving faces {index} of {len(prepared)}")

        logger.info(
            "gallery replaced from %s: enrolled=%s skipped=%s examined=%s",
            folder_relative,
            enrolled,
            skipped,
            len(images),
        )
        return {
            "folder": folder_relative,
            "examined": len(images),
            "enrolled": enrolled,
            "skipped": skipped,
        }
    finally:
        shutil.rmtree(stage, ignore_errors=True)

import logging
import os
import shutil
import time
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpeg", ".mpg"}
SKIP_DIRS = {"jobs"}


class PathError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def data_root() -> Path:
    return Path(settings.data_dir).resolve()


def ensure_data_dirs() -> None:
    root = data_root()
    for name in ("photos", "sources", "jobs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    Path(settings.photo_dir).mkdir(parents=True, exist_ok=True)


def purge_old_jobs(max_age_seconds: int = 86400) -> None:
    root = data_root() / "jobs"
    if not root.is_dir():
        return
    cutoff = time.time() - max_age_seconds
    for child in root.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child)
        except OSError:
            logger.warning("could not remove old job %s", child, exc_info=True)


def resolve_inside(relative: str, *, must_exist: bool = True) -> Path:
    root = data_root()
    raw = (relative or "").strip().replace("\\", "/").lstrip("/")
    if raw.startswith("../") or "/../" in f"/{raw}/" or raw == "..":
        raise PathError("That path is outside the data directory.", 400)
    if ":" in raw.split("/", 1)[0]:
        raise PathError("That path is outside the data directory.", 400)
    candidate = root if raw in ("", ".") else (root / raw).resolve()
    if candidate != root and root not in candidate.parents:
        raise PathError("That path is outside the data directory.", 400)
    if must_exist and not candidate.exists():
        raise PathError("That folder was not found. Put it inside the data directory.", 404)
    return candidate


def relative_to_data(path: Path) -> str:
    return path.resolve().relative_to(data_root()).as_posix()


def safe_parts(relative: str) -> list[str]:
    parts: list[str] = []
    for part in (relative or "").replace("\\", "/").split("/"):
        if part in ("", ".", "..") or part.startswith("."):
            continue
        if ":" in part:
            continue
        parts.append(part)
    return parts


def iter_images(folder: Path):
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        dirnames[:] = [
            name for name in dirnames if not name.startswith(".") and name not in SKIP_DIRS
        ]
        for name in filenames:
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            if path.suffix.lower() in IMAGE_EXT:
                yield path


def count_images(folder: Path, limit: int) -> tuple[int, bool]:
    count = 0
    for _ in iter_images(folder):
        count += 1
        if count > limit:
            return count, True
    return count, False


def list_folder(relative: str) -> dict:
    folder = resolve_inside(relative, must_exist=True)
    if not folder.is_dir():
        raise PathError("That path is not a folder.", 400)
    root = data_root()
    directories = []
    for child in sorted(folder.iterdir(), key=lambda item: item.name.lower()):
        if child.name.startswith(".") or child.name in SKIP_DIRS:
            continue
        if child.is_dir() and not child.is_symlink():
            directories.append({"name": child.name, "path": relative_to_data(child)})
    image_count, truncated = count_images(folder, settings.max_folder_files)
    parent = None
    if folder != root:
        parent = "" if folder.parent == root else relative_to_data(folder.parent)
    return {
        "path": "" if folder == root else relative_to_data(folder),
        "parent": parent,
        "directories": directories,
        "image_count": image_count,
        "truncated": truncated,
    }

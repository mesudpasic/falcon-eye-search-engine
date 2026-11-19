import io
import logging
import os
import threading
import warnings
from dataclasses import dataclass

import numpy as np
import onnxruntime
from insightface.app import FaceAnalysis
from PIL import Image, ImageOps

from app.config import settings

logger = logging.getLogger(__name__)

# InsightFace still calls a scikit-image API that warns on every face.
warnings.filterwarnings(
    "ignore",
    message=r"`estimate` is deprecated",
    category=FutureWarning,
)

MIN_FACE_PX = 48


class ImageRejected(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class DetectedFace:
    embedding: np.ndarray
    box: tuple[float, float, float, float]
    faces_found: int


@dataclass(frozen=True)
class EmbeddedFace:
    embedding: np.ndarray
    box: tuple[float, float, float, float]
    pixel_box: tuple[int, int, int, int]


def _decode(data: bytes) -> np.ndarray:
    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        image = image.convert("RGB")
    except Exception as exc:
        raise ImageRejected("unreadable", "That file could not be read as an image.") from exc
    if max(image.size) > settings.max_image_edge:
        scale = settings.max_image_edge / max(image.size)
        image = image.resize(
            (
                max(1, round(image.width * scale)),
                max(1, round(image.height * scale)),
            ),
            Image.Resampling.LANCZOS,
        )
    rgb = np.asarray(image, dtype=np.uint8)
    return np.ascontiguousarray(rgb[:, :, ::-1])


def _area(face) -> float:
    x1, y1, x2, y2 = face.bbox
    return max(0.0, float(x2 - x1)) * max(0.0, float(y2 - y1))


class FaceEngine:
    """One shared InsightFace session.

    Calls are serialized. ONNX Runtime sessions are not treated as thread-safe,
    and a single upload only needs one embedding.
    """

    def __init__(self) -> None:
        providers = settings.provider_list or ["CPUExecutionProvider"]
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
        options.inter_op_num_threads = 1
        logger.info(
            "loading face model %s with %s",
            settings.model_name,
            ", ".join(providers),
        )
        kwargs = {
            "name": settings.model_name,
            "root": settings.insightface_root,
            "allowed_modules": ["detection", "recognition"],
            "providers": providers,
            "sess_options": options,
        }
        try:
            self.app = FaceAnalysis(**kwargs)
        except TypeError:
            kwargs.pop("sess_options")
            self.app = FaceAnalysis(**kwargs)
        self.app.prepare(
            ctx_id=-1,
            det_thresh=0.5,
            det_size=(settings.det_size, settings.det_size),
        )
        self._lock = threading.Lock()
        logger.info("face model ready")

    def describe(self, data: bytes) -> DetectedFace:
        image = _decode(data)
        with self._lock:
            faces = self.app.get(image)
        if not faces:
            raise ImageRejected(
                "no_face",
                "No face was found. Use a photo where the eyes and head are visible. "
                "A mask over the nose and mouth still works.",
            )
        height, width = image.shape[:2]
        best = max(faces, key=_area)
        x1, y1, x2, y2 = (float(value) for value in best.bbox)
        if (x2 - x1) < MIN_FACE_PX or (y2 - y1) < MIN_FACE_PX:
            raise ImageRejected(
                "too_small",
                "A face was found, but it is too small to recognize. Use a closer portrait.",
            )
        if getattr(best, "normed_embedding", None) is None:
            raise RuntimeError("The recognition model did not return an embedding.")
        embedding = np.asarray(best.normed_embedding, dtype=np.float32).reshape(-1)
        if embedding.shape != (settings.embedding_dim,):
            raise RuntimeError(
                f"Expected a {settings.embedding_dim}-d embedding, got {embedding.shape[0]}."
            )
        norm = float(np.linalg.norm(embedding))
        if norm == 0:
            raise ImageRejected(
                "no_face",
                "No usable face was found in that photo.",
            )
        embedding = np.ascontiguousarray(embedding / norm, dtype=np.float32)
        box = (
            min(1.0, max(0.0, x1 / width)),
            min(1.0, max(0.0, y1 / height)),
            min(1.0, max(0.0, x2 / width)),
            min(1.0, max(0.0, y2 / height)),
        )
        return DetectedFace(embedding=embedding, box=box, faces_found=len(faces))

    def embed_bytes(self, data: bytes, min_px: int = MIN_FACE_PX) -> list[EmbeddedFace]:
        """Every usable face in a still image. Empty when nothing qualifies."""
        return self.embed_array(_decode(data), min_px=min_px)

    def embed_array(self, image: np.ndarray, min_px: int = MIN_FACE_PX) -> list[EmbeddedFace]:
        """Every usable face in a BGR frame. Used for folders and video frames."""
        if image is None or image.ndim != 3 or image.shape[2] != 3 or image.size == 0:
            return []
        height, width = image.shape[:2]
        with self._lock:
            faces = self.app.get(image)
        found: list[EmbeddedFace] = []
        for face in faces:
            x1, y1, x2, y2 = (float(value) for value in face.bbox)
            if (x2 - x1) < min_px or (y2 - y1) < min_px:
                continue
            if getattr(face, "normed_embedding", None) is None:
                continue
            embedding = np.asarray(face.normed_embedding, dtype=np.float32).reshape(-1)
            if embedding.shape != (settings.embedding_dim,):
                continue
            norm = float(np.linalg.norm(embedding))
            if norm == 0:
                continue
            embedding = np.ascontiguousarray(embedding / norm, dtype=np.float32)
            box = (
                min(1.0, max(0.0, x1 / width)),
                min(1.0, max(0.0, y1 / height)),
                min(1.0, max(0.0, x2 / width)),
                min(1.0, max(0.0, y2 / height)),
            )
            pixel = (
                max(0, int(x1)),
                max(0, int(y1)),
                min(width, int(round(x2))),
                min(height, int(round(y2))),
            )
            found.append(EmbeddedFace(embedding=embedding, box=box, pixel_box=pixel))
        return found

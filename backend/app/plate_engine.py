import logging
import threading
from dataclasses import dataclass

import numpy as np

from app.matching import mean_confidence, normalize_plate

logger = logging.getLogger(__name__)

MIN_PLATE_LEN = 4
MAX_PLATE_LEN = 12
MIN_OCR_CONFIDENCE = 0.3


@dataclass(frozen=True)
class PlateRead:
    text: str
    raw: str
    confidence: float
    box: tuple[int, int, int, int]


class PlateEngine:
    """License-plate detector and OCR. The models load on the first video."""

    def __init__(self) -> None:
        self._alpr = None
        self._load_lock = threading.Lock()
        self._lock = threading.Lock()

    def _load(self):
        if self._alpr is not None:
            return self._alpr
        with self._load_lock:
            if self._alpr is None:
                logger.info("loading license plate models")
                from fast_alpr import ALPR

                self._alpr = ALPR(
                    detector_model="yolo-v9-t-384-license-plate-end2end",
                    ocr_model="cct-xs-v2-global-model",
                    ocr_device="cpu",
                    detector_providers=["CPUExecutionProvider"],
                    ocr_providers=["CPUExecutionProvider"],
                )
                logger.info("license plate models ready")
        return self._alpr

    def read(self, image: np.ndarray) -> list[PlateRead]:
        try:
            alpr = self._load()
        except Exception as exc:
            raise RuntimeError("License plate recognition is unavailable.") from exc
        with self._lock:
            results = alpr.predict(image)
        height, width = image.shape[:2]
        found: list[PlateRead] = []
        for result in results:
            ocr = result.ocr
            if ocr is None or not getattr(ocr, "text", None):
                continue
            raw = str(ocr.text).strip()
            text = normalize_plate(raw)
            if len(text) < MIN_PLATE_LEN or len(text) > MAX_PLATE_LEN:
                continue
            confidence = mean_confidence(getattr(ocr, "confidence", None))
            if confidence < MIN_OCR_CONFIDENCE:
                continue
            bbox = result.detection.bounding_box
            box = (
                max(0, int(bbox.x1)),
                max(0, int(bbox.y1)),
                min(width, int(bbox.x2)),
                min(height, int(bbox.y2)),
            )
            found.append(PlateRead(text=text, raw=raw, confidence=confidence, box=box))
        return found

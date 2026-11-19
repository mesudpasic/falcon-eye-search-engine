import logging
from pathlib import Path
from urllib.parse import quote

import cv2
import numpy as np

from app.config import settings
from app.face_engine import FaceEngine
from app.jobs import ScanError
from app.matching import add_hit, clock_label, plates_match
from app.paths import data_root
from app.plate_engine import PlateEngine

logger = logging.getLogger(__name__)

VIDEO_MIN_FACE = 24


def _open_capture(path: Path):
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        capture.release()
        raise ScanError("That video could not be opened. Use mp4, mov, avi, mkv, or webm.")
    return capture


def _timing(capture) -> tuple[float, float, float]:
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
    if fps < 1 or fps > 240:
        fps = 25.0
    frames = float(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = frames / fps if frames > 0 else 0.0
    return fps, frames, duration


def _limit_edge(image: np.ndarray) -> np.ndarray:
    height, width = image.shape[:2]
    longest = max(height, width)
    edge = settings.max_image_edge
    if longest <= edge:
        return image
    scale = edge / longest
    return cv2.resize(
        image,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )


def _marked(frame: np.ndarray, box: tuple[int, int, int, int], text: str) -> np.ndarray:
    image = frame.copy()
    x1, y1, x2, y2 = box
    cv2.rectangle(image, (x1, y1), (x2, y2), (70, 170, 230), 2)
    if text:
        cv2.putText(
            image,
            text,
            (x1, max(22, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
    return image


def _save_snapshot(work_dir: Path, index: int, frame: np.ndarray, box, text: str) -> str:
    name = f"hit-{index:03d}.jpg"
    path = work_dir / name
    cv2.imwrite(str(path), _marked(frame, box, text), [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    relative = path.resolve().relative_to(data_root()).as_posix()
    return f"/api/media?path={quote(relative)}"


def _hits_from_tracks(tracks, work_dir: Path, duration: float, sample_fps: float) -> list[dict]:
    pad = 0.5 / sample_fps
    hits = []
    for index, track in enumerate(tracks):
        frame, box, display = track.payload
        end = track.end
        if duration:
            end = min(duration, max(end, track.start + pad))
        url = _save_snapshot(work_dir, index, frame, box, display)
        score = max(0.0, min(1.0, float(track.score)))
        hits.append(
            {
                "label": display,
                "start_seconds": round(track.start, 3),
                "end_seconds": round(end, 3),
                "start_label": clock_label(track.start),
                "end_label": clock_label(end),
                "score": round(score, 6),
                "match_percent": round(score * 100, 1),
                "snapshot_url": url,
            }
        )
    return hits


def _scan_frames(video_path: Path, sample_fps: float, progress, on_frame):
    capture = _open_capture(video_path)
    try:
        fps, frames, duration = _timing(capture)
        step = max(1, int(round(fps / sample_fps)))
        expected = max(1, int(frames / step)) if frames else 0
        index = 0
        checked = 0
        last_time = 0.0
        while True:
            take = index % step == 0
            if take:
                ok, frame = capture.read()
            else:
                ok = capture.grab()
                frame = None
            if not ok:
                break
            if take and frame is not None:
                checked += 1
                last_time = index / fps
                on_frame(_limit_edge(frame), last_time)
                if expected:
                    progress(min(0.97, checked / expected), f"Checked {checked} frames")
                elif checked % 5 == 0:
                    progress(0.5, f"Checked {checked} frames")
            index += 1
        if not duration:
            duration = last_time
        return duration, checked
    finally:
        capture.release()


def scan_face_video(
    *,
    engine: FaceEngine,
    video_path: Path,
    work_dir: Path,
    embedding: np.ndarray,
    min_similarity: float,
    sample_fps: float,
    progress,
) -> dict:
    query = np.ascontiguousarray(embedding, dtype=np.float32)
    tracks = []
    gap = max(1.0, 1.5 / sample_fps)

    def on_frame(frame, timestamp: float) -> None:
        best = None
        for face in engine.embed_array(frame, min_px=VIDEO_MIN_FACE):
            score = float(np.dot(query, face.embedding))
            if score < min_similarity:
                continue
            if best is None or score > best[0]:
                best = (score, face.pixel_box)
        if best is None:
            return
        score, box = best
        add_hit(
            tracks,
            time=timestamp,
            score=score,
            label="face",
            payload=(frame.copy(), box, ""),
            gap=gap,
        )

    progress(0.01, "Opening the video")
    duration, checked = _scan_frames(video_path, sample_fps, progress, on_frame)
    hits = _hits_from_tracks(tracks, work_dir, duration, sample_fps)
    note = "" if hits else "That face was not found above the similarity floor in the sampled frames."
    logger.info("face video checked=%s hits=%s", checked, len(hits))
    return {
        "duration_seconds": round(duration, 3),
        "frames_checked": checked,
        "sample_fps": sample_fps,
        "query": None,
        "note": note,
        "hits": hits,
    }


def scan_plate_video(
    *,
    plates: PlateEngine,
    video_path: Path,
    work_dir: Path,
    plate_query: str,
    sample_fps: float,
    progress,
) -> dict:
    tracks = []
    others: set[str] = set()
    gap = max(1.0, 1.5 / sample_fps)
    query = plate_query.strip()

    def on_frame(frame, timestamp: float) -> None:
        try:
            reads = plates.read(frame)
        except RuntimeError as exc:
            raise ScanError(str(exc)) from exc
        for plate in reads:
            if query and not plates_match(query, plate.text):
                others.add(plate.text)
                continue
            add_hit(
                tracks,
                time=timestamp,
                score=plate.confidence,
                label=plate.text,
                payload=(frame.copy(), plate.box, plate.raw or plate.text),
                gap=gap,
            )

    progress(0.01, "Opening the video")
    duration, checked = _scan_frames(video_path, sample_fps, progress, on_frame)
    hits = _hits_from_tracks(tracks, work_dir, duration, sample_fps)
    if hits:
        note = ""
    elif query and others:
        note = f"Plates were read, but none were close to {query}."
    else:
        note = "No license plate was read in the sampled frames."
    logger.info("plate video checked=%s hits=%s others=%s", checked, len(hits), len(others))
    return {
        "duration_seconds": round(duration, 3),
        "frames_checked": checked,
        "sample_fps": sample_fps,
        "query": query or None,
        "note": note,
        "hits": hits,
    }

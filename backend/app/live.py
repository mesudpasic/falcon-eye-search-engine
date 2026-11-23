"""Watch one camera or network stream and report faces and plates as they appear."""

import asyncio
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import cv2
import numpy as np

from app.jobs import ScanError
from app.live_source import LiveSource
from app.matching import add_hit, clock_label, plates_match
from app.paths import data_root
from app.scan_video import VIDEO_MIN_FACE, _limit_edge, _marked

logger = logging.getLogger(__name__)

MAX_WATCH_SECONDS = 4 * 60 * 60
PREVIEW_EDGE = 960
MAX_TRACKS = 80
MAX_HITS = 40
MAX_REVISIONS = 12
SNAPSHOT_GAP = 0.5
_OPEN_MESSAGES = {
    "Opening the camera",
    "Watching",
    "Starting",
    "Waiting for the other scan to finish",
    "Camera dropped. Reconnecting…",
}


class LiveSession:
    def __init__(
        self,
        *,
        source_label: str,
        watch: str,
        sample_fps: float,
        plate_query: str,
        min_similarity: float,
        empty_note: str,
    ) -> None:
        self.id = uuid.uuid4()
        self.source_label = source_label
        self.watch = watch
        self.sample_fps = sample_fps
        self.plate_query = plate_query
        self.min_similarity = min_similarity
        self.empty_note = empty_note
        self.embedding: np.ndarray | None = None
        self.task = None
        self.saw_other = False
        self.tracks: list = []
        self.meta: dict[int, dict] = {}
        self._index = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.status = "starting"
        self.message = "Opening the camera"
        self.error: str | None = None
        self.note = ""
        self.frames_checked = 0
        self.elapsed = 0.0
        self.preview_rev = 0
        self._preview: bytes | None = None
        self._hits: list[dict] = []

    def request_stop(self) -> None:
        self._stop.set()

    def stopped(self) -> bool:
        return self._stop.is_set()

    def take_index(self) -> int:
        self._index += 1
        return self._index

    def set_message(self, message: str) -> None:
        with self._lock:
            if self._stop.is_set() or self.status in {"error", "stopped"}:
                return
            self.message = message

    def set_watching(self) -> None:
        with self._lock:
            if self._stop.is_set() or self.status in {"error", "stopped"}:
                return
            self.status = "live"
            self.message = "Watching"
            self.error = None

    def fail(self, message: str) -> None:
        with self._lock:
            if self._stop.is_set() or self.status in {"stopped", "error"}:
                return
            self.status = "error"
            self.error = message
            self.message = message

    def mark_stopped(self) -> None:
        with self._lock:
            if self.status in {"error", "stopped"}:
                return
            self.status = "stopped"
            if not self._hits and self.message == "Stopped after 4 hours.":
                self.note = self._empty_note()
            elif not self._hits and not self._stop.is_set():
                self.note = self._empty_note()
            if self.message in _OPEN_MESSAGES:
                self.message = "Stopped"

    def _empty_note(self) -> str:
        if self.saw_other and self.plate_query and self.watch != "faces":
            return f"Plates were read, but none were close to {self.plate_query}."
        return self.empty_note

    def publish(self, *, frames: int, elapsed: float, hits: list[dict], preview: bytes | None) -> None:
        with self._lock:
            self.frames_checked = frames
            self.elapsed = elapsed
            self._hits = hits
            if preview is not None:
                self._preview = preview
                self.preview_rev += 1

    def preview_bytes(self) -> bytes | None:
        with self._lock:
            return self._preview

    def view(self) -> dict:
        with self._lock:
            return {
                "session_id": self.id,
                "status": self.status,
                "message": self.message,
                "error": self.error,
                "source": self.source_label,
                "watch": self.watch,
                "sample_fps": self.sample_fps,
                "frames_checked": self.frames_checked,
                "elapsed_seconds": round(self.elapsed, 3),
                "elapsed_label": clock_label(self.elapsed),
                "preview_rev": self.preview_rev,
                "note": self.note,
                "hits": [dict(hit) for hit in self._hits],
            }


class LiveHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: LiveSession | None = None

    def install(self, session: LiveSession) -> None:
        with self._lock:
            previous = self._current
            self._current = session
        if previous is not None:
            previous.request_stop()

    def get(self, session_id: uuid.UUID) -> LiveSession | None:
        with self._lock:
            current = self._current
        if current is not None and current.id == session_id:
            return current
        return None

    def current(self) -> LiveSession | None:
        with self._lock:
            return self._current

    async def shutdown(self) -> None:
        session = self.current()
        if session is None:
            return
        session.request_stop()
        task = session.task
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=8)
        except TimeoutError:
            logger.warning("live session %s did not stop in time", session.id)


def run_live(session: LiveSession, source: LiveSource, work_dir: Path, engine, plates) -> None:
    """Block until the watch is stopped, the stream ends, or four hours pass."""
    if session.embedding is not None:
        session.embedding = np.ascontiguousarray(session.embedding, dtype=np.float32)
    if session.watch in {"faces", "both"} and session.embedding is None:
        session.fail("Choose a face to look for.")
        return
    key = "OPENCV_FFMPEG_CAPTURE_OPTIONS"
    previous = os.environ.get(key)
    if source.kind == "url":
        os.environ[key] = "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;500000"
    opened_once = False
    try:
        for attempt in range(3):
            if session.stopped():
                session.mark_stopped()
                return
            try:
                capture = _open_capture(source)
            except ScanError as exc:
                if not opened_once:
                    logger.info("live %s could not open %s", session.id, source.label)
                    session.fail(exc.message)
                    return
                if attempt == 2 or session.stopped():
                    if session.stopped():
                        session.mark_stopped()
                    else:
                        session.fail("The camera stopped sending pictures.")
                    return
                session.set_message("Camera dropped. Reconnecting…")
                if not _wait(session, 1.0):
                    session.mark_stopped()
                    return
                continue
            opened_once = True
            session.set_watching()
            logger.info("live %s watching %s (%s)", session.id, source.label, session.watch)
            try:
                outcome = _pump(capture, session, work_dir, engine, plates)
            finally:
                if not getattr(session, "reader_stuck", False):
                    _release(capture)
            if outcome == "stop" or session.stopped():
                session.mark_stopped()
                return
            if attempt == 2:
                session.fail("The camera stopped sending pictures.")
                return
            session.set_message("Camera dropped. Reconnecting…")
            if not _wait(session, 1.0):
                session.mark_stopped()
                return
    except Exception:
        logger.exception("live %s failed", session.id)
        session.fail("The camera watch failed.")
    finally:
        if source.kind == "url":
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def _open_params() -> list[int]:
    params: list[int] = []
    if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
        params.extend([int(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC), 8000])
    if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
        params.extend([int(cv2.CAP_PROP_READ_TIMEOUT_MSEC), 5000])
    return params


def _open_capture(source: LiveSource):
    params = _open_params()
    if source.kind == "device":
        capture = _capture(int(source.value), cv2.CAP_ANY, params)
        if not capture.isOpened() and sys.platform == "win32" and hasattr(cv2, "CAP_DSHOW"):
            capture.release()
            capture = _capture(int(source.value), cv2.CAP_DSHOW, params)
    else:
        capture = _capture(str(source.value), cv2.CAP_FFMPEG, params)
    if capture.isOpened():
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if not capture.isOpened():
        capture.release()
        if source.kind == "device":
            raise ScanError(
                "No camera with that number was found. "
                "A webcam or capture card has to be visible to the API. "
                "An RTSP address works over the network."
            )
        raise ScanError("That camera could not be opened. Check the address and that this machine can reach it.")
    return capture


def _capture(target, backend: int, params: list[int]):
    if params:
        try:
            return cv2.VideoCapture(target, backend, params)
        except TypeError:
            logger.info("this OpenCV build ignores camera timeouts")
    return cv2.VideoCapture(target, backend)


def _release(capture) -> None:
    try:
        capture.release()
    except Exception:
        logger.warning("could not release the camera", exc_info=True)


def _wait(session: LiveSession, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if session.stopped():
            return False
        time.sleep(0.05)
    return True


def _pump(capture, session: LiveSession, work_dir: Path, engine, plates) -> str:
    """Read the latest frame a few times a second. Returns "stop" or "drop"."""
    guard = threading.Lock()
    latest: dict = {"frame": None}
    dead = threading.Event()
    stop_reader = threading.Event()

    def reader() -> None:
        misses = 0
        first_miss = 0.0
        while not session.stopped() and not stop_reader.is_set():
            ok, frame = capture.read()
            if ok and frame is not None:
                misses = 0
                with guard:
                    latest["frame"] = frame.copy()
                continue
            now = time.monotonic()
            if misses == 0:
                first_miss = now
            misses += 1
            if misses >= 8 or now - first_miss >= 8:
                dead.set()
                return
            time.sleep(0.05)
        dead.set()

    thread = threading.Thread(target=reader, name=f"live-{session.id.hex[:8]}", daemon=True)
    thread.start()
    if getattr(session, "origin", None) is None:
        session.origin = time.monotonic()
    try:
        if not _await_first_frame(session, guard, latest, dead, thread):
            return "stop" if session.stopped() else "drop"
        interval = 1.0 / session.sample_fps
        next_at = time.monotonic()
        checked = session.frames_checked
        while not session.stopped():
            now = time.monotonic()
            elapsed = now - session.origin
            if elapsed >= MAX_WATCH_SECONDS:
                session.set_message("Stopped after 4 hours.")
                session.request_stop()
                return "stop"
            if now < next_at:
                time.sleep(min(0.05, next_at - now))
                continue
            next_at = now + interval
            with guard:
                frame = latest["frame"]
                latest["frame"] = None
            if frame is None:
                if dead.is_set() and not thread.is_alive():
                    return "drop"
                continue
            checked += 1
            try:
                _consume(
                    session,
                    frame,
                    time.monotonic() - session.origin,
                    checked,
                    work_dir,
                    engine,
                    plates,
                )
            except ScanError as exc:
                session.fail(exc.message)
                return "stop"
        return "stop"
    finally:
        stop_reader.set()
        thread.join(timeout=8)
        if thread.is_alive():
            session.reader_stuck = True
            logger.warning("live %s reader did not stop", session.id)


def _await_first_frame(session, guard, latest, dead, thread) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not session.stopped():
        with guard:
            got = latest["frame"] is not None
        if got:
            return True
        if dead.is_set() and not thread.is_alive():
            return False
        time.sleep(0.05)
    with guard:
        return latest["frame"] is not None


def _consume(session, frame, elapsed: float, checked: int, work_dir: Path, engine, plates) -> None:
    image = _limit_edge(frame)
    marks: list[tuple[tuple[int, int, int, int], str]] = []
    gap = max(1.0, 1.5 / session.sample_fps)
    if session.watch in {"faces", "both"} and session.embedding is not None:
        _consume_face(session, image, elapsed, work_dir, engine, gap, marks)
    if session.watch in {"plates", "both"}:
        _consume_plates(session, image, elapsed, work_dir, plates, gap, marks)
    _trim_tracks(session, elapsed, gap)
    hits = []
    for track in session.tracks:
        hit = _hit_dict(track)
        if hit is not None:
            hits.append(hit)
    hits.sort(key=lambda item: (item["end_seconds"], item["start_seconds"]), reverse=True)
    session.publish(
        frames=checked,
        elapsed=elapsed,
        hits=hits[:MAX_HITS],
        preview=_encode_preview(_annotate(image, marks)),
    )


def _consume_face(session, image, elapsed, work_dir, engine, gap, marks) -> None:
    query = session.embedding
    best = None
    for face in engine.embed_array(image, min_px=VIDEO_MIN_FACE):
        if face.embedding.shape != query.shape:
            continue
        score = float(np.dot(query, face.embedding))
        if score < session.min_similarity:
            continue
        if best is None or score > best[0]:
            best = (score, face.pixel_box)
    if best is None:
        return
    score, box = best
    _record(session, work_dir, image, elapsed, score, "Face", "face", box, "", gap)
    marks.append((box, f"{round(score * 100)}%"))


def _consume_plates(session, image, elapsed, work_dir, plates, gap, marks) -> None:
    try:
        reads = plates.read(image)
    except RuntimeError as exc:
        raise ScanError(str(exc)) from exc
    query = session.plate_query
    for plate in reads:
        if query and not plates_match(query, plate.text):
            session.saw_other = True
            continue
        text = plate.raw or plate.text
        _record(session, work_dir, image, elapsed, plate.confidence, plate.text, "plate", plate.box, text, gap)
        marks.append((plate.box, text))


def _record(session, work_dir, frame, elapsed, score, label, kind, box, text, gap) -> None:
    token = object()
    track = add_hit(session.tracks, time=elapsed, score=score, label=label, payload=token, gap=gap)
    if track.payload is not token:
        return
    meta = session.meta.get(id(track))
    if meta is None:
        meta = {"index": session.take_index(), "revision": 0, "kind": kind, "snapshot_url": "", "written_at": -999.0}
        session.meta[id(track)] = meta
    due = meta["revision"] == 0 or elapsed - meta["written_at"] >= SNAPSHOT_GAP
    if due and meta["revision"] < MAX_REVISIONS:
        meta["revision"] += 1
        meta["written_at"] = elapsed
        meta["snapshot_url"] = _write_hit(work_dir, meta["index"], meta["revision"], frame, box, text)
    meta["kind"] = kind
    track.payload = meta


def _write_hit(work_dir: Path, index: int, revision: int, frame, box, text: str) -> str:
    name = f"hit-{index:03d}-{revision:02d}.jpg"
    path = work_dir / name
    if not cv2.imwrite(str(path), _marked(frame, box, text), [int(cv2.IMWRITE_JPEG_QUALITY), 85]):
        return ""
    relative = path.resolve().relative_to(data_root()).as_posix()
    return f"/api/media?path={quote(relative)}"


def _trim_tracks(session, elapsed: float, gap: float) -> None:
    tracks = session.tracks
    if len(tracks) <= MAX_TRACKS:
        return
    closed = [track for track in tracks if elapsed - track.end > gap]
    closed.sort(key=lambda track: track.end)
    while len(tracks) > MAX_TRACKS and closed:
        old = closed.pop(0)
        tracks.remove(old)
        session.meta.pop(id(old), None)


def _hit_dict(track) -> dict | None:
    meta = track.payload if isinstance(track.payload, dict) else None
    if not meta or not meta.get("snapshot_url"):
        return None
    score = max(0.0, min(1.0, float(track.score)))
    return {
        "label": track.label,
        "kind": meta.get("kind") or "face",
        "start_seconds": round(float(track.start), 3),
        "end_seconds": round(float(track.end), 3),
        "start_label": clock_label(track.start),
        "end_label": clock_label(track.end),
        "score": round(score, 6),
        "match_percent": round(score * 100, 1),
        "snapshot_url": meta["snapshot_url"],
    }


def _annotate(frame, marks):
    if not marks:
        return frame
    image = frame
    for box, text in marks:
        image = _marked(image, box, text)
    return image


def _encode_preview(frame) -> bytes | None:
    height, width = frame.shape[:2]
    longest = max(height, width)
    if longest > PREVIEW_EDGE:
        scale = PREVIEW_EDGE / longest
        frame = cv2.resize(
            frame,
            (max(1, round(width * scale)), max(1, round(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 72])
    if not ok:
        return None
    return encoded.tobytes()

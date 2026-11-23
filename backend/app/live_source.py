import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from app.jobs import ScanError

_DEVICE = re.compile(r"^(?:camera:)?(\d{1,2})$", re.IGNORECASE)
_SCHEMES = {"rtsp", "rtsps", "rtmp", "rtmps", "http", "https", "udp", "tcp"}
_MAX_LEN = 500


@dataclass(frozen=True)
class LiveSource:
    kind: str
    value: str | int
    label: str


def parse_source(raw: str) -> LiveSource:
    """A stream URL, or a camera number such as 0 or camera:1.

    The label is safe to show and to log. The value is what OpenCV should open,
    so a password in an RTSP URL stays in the value and is left out of the label.
    """
    text = (raw or "").strip()
    if not text:
        raise ScanError("Enter a camera address or a camera number.")
    if len(text) > _MAX_LEN or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ScanError("That camera address is not usable.")
    device = _DEVICE.fullmatch(text)
    if device:
        index = int(device.group(1))
        if index > 16:
            raise ScanError("Use a camera number from 0 to 16, or a stream address.")
        return LiveSource("device", index, f"Camera {index}")
    if "://" not in text or any(char.isspace() for char in text):
        raise ScanError("Use an RTSP, RTMP, HTTP, or UDP address, or a camera number such as 0.")
    parts = urlsplit(text)
    if parts.scheme.lower() not in _SCHEMES or not parts.netloc:
        raise ScanError("Use an RTSP, RTMP, HTTP, or UDP address, or a camera number such as 0.")
    return LiveSource("url", text, _redact(parts))


def _redact(parts) -> str:
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, ""))


def empty_note(watch: str, plate: str) -> str:
    wanted = plate.strip()
    if watch == "faces":
        return "That face was not seen above the similarity floor."
    if watch == "plates":
        if wanted:
            return f"No plate close to {wanted} was read."
        return "No license plate was read."
    if wanted:
        return "That face was not seen, and no plate close to the one you typed was read."
    return "That face was not seen, and no license plate was read."

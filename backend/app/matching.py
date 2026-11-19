import re
from dataclasses import dataclass


_PLATE = re.compile(r"[^A-Z0-9]")


def normalize_plate(value: str | None) -> str:
    return _PLATE.sub("", (value or "").upper())


def levenshtein(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, 1):
        current = [i]
        for j, right_char in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[j] + 1,
                    previous[j - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def plates_match(query: str, found: str) -> bool:
    """True when two plate readings are equal or one character apart."""
    left = normalize_plate(query)
    right = normalize_plate(found)
    if not left or not right or abs(len(left) - len(right)) > 1:
        return False
    return levenshtein(left, right) <= 1


def mean_confidence(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, (list, tuple)):
        numbers = [float(item) for item in value if isinstance(item, (int, float))]
        number = sum(numbers) / len(numbers) if numbers else 0.0
    else:
        return 0.0
    if number > 1:
        number = number / 100.0
    return max(0.0, min(1.0, number))


def clock_label(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


@dataclass
class Track:
    start: float
    end: float
    score: float
    label: str
    payload: object


def add_hit(tracks: list[Track], *, time: float, score: float, label: str, payload, gap: float) -> Track:
    """Extend the latest track with this label, or start one.

    A different label at the same moment stays on its own track, so two plates
    in one frame do not collapse into each other.
    """
    candidate: Track | None = None
    for track in tracks:
        if track.label == label and time + 1e-6 >= track.start and time - track.end <= gap:
            candidate = track
    if candidate is not None:
        candidate.end = max(candidate.end, time)
        if score >= candidate.score:
            candidate.score = score
            candidate.payload = payload
        return candidate
    track = Track(start=time, end=time, score=score, label=label, payload=payload)
    tracks.append(track)
    return track

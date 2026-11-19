"""Enroll freely licensed portraits from Wikimedia Commons.

File bytes and face embeddings are stored separately. The JPEG is saved as
data/photos/{sha1}.jpg. The database row keeps that SHA-1 plus a 512-d
recognition embedding used only for similarity.
"""

import argparse
import asyncio
import hashlib
import html
import io
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from PIL import Image, ImageOps

from app.config import settings
from app.db import (
    count_faces,
    insert_face,
    insert_skip,
    load_seen_ids,
    open_pool,
    reset_gallery,
)
from app.face_engine import FaceEngine, ImageRejected
from app.log import configure_logging

logger = logging.getLogger(__name__)

USER_AGENT = (
    "FalconEyeSearch/1.0 (https://www.setec.ba; https://www.wikidata.org/wiki/Wikidata:Data_access; educational demo) "
    "httpx/0.28"
)
SPARQL_URL = "https://query.wikidata.org/sparql"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
MAX_EDGE = 960
DOWNLOAD_LIMIT = 12 * 1024 * 1024

# One photo per person, taken from the person's Wikimedia Commons image (P18).
OCCUPATIONS = [
    ("Q33999", "actor"),
    ("Q10800557", "film actor"),
    ("Q10798782", "television actor"),
    ("Q177220", "singer"),
    ("Q488205", "singer-songwriter"),
    ("Q2252262", "rapper"),
    ("Q639669", "musician"),
    ("Q855091", "guitarist"),
    ("Q36834", "composer"),
    ("Q2526255", "film director"),
    ("Q28389", "screenwriter"),
    ("Q753110", "voice actor"),
    ("Q947873", "television presenter"),
    ("Q4610556", "model"),
    ("Q2066131", "athlete"),
    ("Q82955", "politician"),
    ("Q1028181", "painter"),
    ("Q483501", "artist"),
    ("Q33231", "photographer"),
    ("Q1930187", "journalist"),
]

FILENAME_SKIP = ("signature", "autograph", "coat_of_arms", "logo", "flag_of", "icon")
TAG = re.compile(r"<[^>]+>")
SPACE = re.compile(r"\s+")
QID = re.compile(r"Q\d+")


@dataclass(frozen=True)
class Candidate:
    qid: str
    name: str
    filename: str


@dataclass(frozen=True)
class RemoteImage:
    qid: str
    name: str
    filename: str
    thumb_url: str
    source_url: str | None
    author: str | None
    license_name: str | None
    license_url: str | None


def title_key(name: str) -> str:
    return name.removeprefix("File:").replace("_", " ").casefold()


def commons_filename(image_url: str) -> str | None:
    path = unquote(urlparse(image_url).path)
    marker = "/Special:FilePath/"
    index = path.find(marker)
    if index == -1:
        return None
    name = path[index + len(marker) :].strip()
    if not name or "/" in name or "?" in name:
        return None
    return name


def clean_text(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    text = html.unescape(TAG.sub(" ", value))
    text = SPACE.sub(" ", text).strip()
    if not text:
        return None
    return text[:limit]


def meta_value(info: dict, key: str, limit: int) -> str | None:
    raw = (info.get("extmetadata") or {}).get(key) or {}
    if not isinstance(raw, dict):
        return None
    return clean_text(raw.get("value"), limit)


def to_jpeg(data: bytes) -> tuple[bytes, int, int] | None:
    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        image = image.convert("RGB")
    except Exception:
        return None
    width, height = image.size
    if max(width, height) > MAX_EDGE:
        scale = MAX_EDGE / max(width, height)
        image = image.resize(
            (max(1, round(width * scale)), max(1, round(height * scale))),
            Image.Resampling.LANCZOS,
        )
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=86)
    return buffer.getvalue(), image.size[0], image.size[1]


def save_jpeg(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".jpg.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def sparql(occupation: str, limit: int, offset: int) -> str:
    if QID.fullmatch(occupation) is None:
        raise ValueError(occupation)
    # Limit the people first, then ask for English labels. Labeling the whole
    # occupation before LIMIT makes Wikidata time out.
    return f"""
SELECT ?person ?personLabel ?image WHERE {{
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
  {{
    SELECT ?person ?image WHERE {{
      ?person wdt:P31 wd:Q5 ;
              wdt:P106 wd:{occupation} ;
              wdt:P18 ?image .
    }}
    LIMIT {int(limit)}
    OFFSET {int(offset)}
  }}
}}
"""


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: list[tuple[str, str]] | None = None,
    data: dict | None = None,
    headers: dict | None = None,
    attempts: int = 4,
) -> httpx.Response:
    delay = 2.0
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = await client.request(
                method,
                url,
                params=params,
                data=data,
                headers=headers,
            )
            if response.status_code in {429, 502, 503, 504}:
                wait = float(response.headers.get("Retry-After", delay))
                await asyncio.sleep(min(max(wait, 1.0), 60.0))
                delay = min(delay * 2, 60.0)
                continue
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            snippet = exc.response.text[:240].replace("\n", " ")
            last_error = RuntimeError(f"HTTP {exc.response.status_code} from {url}: {snippet}")
        except httpx.HTTPError as exc:
            last_error = exc
        if attempt == attempts - 1:
            break
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60.0)
    raise RuntimeError(f"request failed for {url}: {last_error}") from last_error


async def fetch_candidates(
    client: httpx.AsyncClient,
    occupation: str,
    offset: int,
    page_size: int,
) -> tuple[list[Candidate], int]:
    response = await request_with_retry(
        client,
        "POST",
        SPARQL_URL,
        data={"query": sparql(occupation, page_size, offset)},
        headers={"Accept": "application/sparql-results+json"},
    )
    payload = response.json()
    bindings = payload.get("results", {}).get("bindings", [])
    found: list[Candidate] = []
    seen: set[str] = set()
    for row in bindings:
        person = row.get("person", {}).get("value", "")
        qid = person.rsplit("/", 1)[-1]
        name = clean_text(row.get("personLabel", {}).get("value"), 240)
        filename = commons_filename(row.get("image", {}).get("value", ""))
        if QID.fullmatch(qid or "") is None or not name or name == qid or not filename:
            continue
        lowered = filename.lower()
        if any(word in lowered for word in FILENAME_SKIP):
            continue
        if qid in seen:
            continue
        seen.add(qid)
        found.append(Candidate(qid=qid, name=name, filename=filename))
    return found, len(bindings)


async def fetch_metadata(
    client: httpx.AsyncClient,
    candidates: list[Candidate],
) -> list[RemoteImage]:
    images: list[RemoteImage] = []
    for start in range(0, len(candidates), 20):
        chunk = candidates[start : start + 20]
        params = [
            ("action", "query"),
            ("format", "json"),
            ("prop", "imageinfo"),
            ("iiprop", "url|mime|size|extmetadata"),
            ("iiurlwidth", "960"),
            ("maxlag", "5"),
            ("titles", "|".join(f"File:{item.filename}" for item in chunk)),
        ]
        payload = None
        for _ in range(3):
            try:
                response = await request_with_retry(client, "GET", COMMONS_API, params=params)
            except RuntimeError as exc:
                logger.warning("commons metadata failed: %s", exc)
                payload = None
                break
            payload = response.json()
            if payload.get("error", {}).get("code") == "maxlag":
                await asyncio.sleep(5)
                payload = None
                continue
            break
        if not payload or "query" not in payload:
            continue
        by_title = {
            title_key(page.get("title", "")): page
            for page in payload.get("query", {}).get("pages", {}).values()
            if page.get("title")
        }
        for item in chunk:
            page = by_title.get(title_key(item.filename)) or {}
            info_list = page.get("imageinfo") or []
            if not info_list:
                continue
            info = info_list[0]
            mime = info.get("mime") or ""
            if mime not in {"image/jpeg", "image/png", "image/webp", "image/gif", "image/tiff"}:
                continue
            thumb = info.get("thumburl")
            original = info.get("url")
            size = int(info.get("size") or 0)
            download_url = thumb or (original if size and size <= DOWNLOAD_LIMIT else None)
            if not download_url:
                continue
            images.append(
                RemoteImage(
                    qid=item.qid,
                    name=item.name,
                    filename=item.filename,
                    thumb_url=download_url,
                    source_url=info.get("descriptionurl"),
                    author=meta_value(info, "Artist", 500),
                    license_name=meta_value(info, "LicenseShortName", 120),
                    license_url=meta_value(info, "LicenseUrl", 300),
                )
            )
        await asyncio.sleep(0.25)
    return images


async def download(client: httpx.AsyncClient, url: str) -> bytes | None:
    delay = 1.5
    for _ in range(3):
        try:
            async with client.stream(
                "GET",
                url,
                headers={"Accept": "image/*,*/*;q=0.8", "Referer": "https://commons.wikimedia.org/"},
            ) as response:
                if response.status_code in {429, 502, 503, 504}:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                response.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > DOWNLOAD_LIMIT:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError:
            await asyncio.sleep(delay)
            delay *= 2
    return None


async def download_many(
    client: httpx.AsyncClient,
    images: list[RemoteImage],
) -> list[tuple[RemoteImage, bytes | None]]:
    semaphore = asyncio.Semaphore(3)

    async def one(image: RemoteImage) -> tuple[RemoteImage, bytes | None]:
        async with semaphore:
            payload = await download(client, image.thumb_url)
            return image, payload

    return list(await asyncio.gather(*(one(image) for image in images)))


async def remember_skip(pool, seen: set[str], qid: str, reason: str) -> None:
    seen.add(qid)
    await insert_skip(pool, qid, reason)


async def run(target: int, reset: bool) -> int:
    started = time.perf_counter()
    pool = await open_pool()
    engine: FaceEngine | None = None
    try:
        if reset:
            logger.warning("clearing enrolled faces and files in %s", settings.photo_dir)
            await reset_gallery(pool)
            folder = Path(settings.photo_dir)
            for path in folder.glob("*.jpg"):
                path.unlink(missing_ok=True)
            for path in folder.glob("*.jpg.tmp"):
                path.unlink(missing_ok=True)

        enrolled = await count_faces(pool)
        if enrolled >= target:
            logger.info("gallery already has %s faces (target %s)", enrolled, target)
            return 0

        engine = FaceEngine()
        seen = await load_seen_ids(pool)
        skipped = 0
        examined = 0
        initial = enrolled
        logger.info("resuming at %s/%s enrolled faces", enrolled, target)

        timeout = httpx.Timeout(90.0, connect=20.0)
        limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)
        headers = {"User-Agent": USER_AGENT, "Api-User-Agent": USER_AGENT}
        async with httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            headers=headers,
            follow_redirects=True,
        ) as client:
            for occupation, label in OCCUPATIONS:
                offset = 0
                if enrolled >= target:
                    break
                logger.info("occupation %s (%s) from offset %s", label, occupation, offset)
                pages = 0
                while enrolled < target and examined < 80000 and pages < 800:
                    pages += 1
                    try:
                        candidates, raw_count = await fetch_candidates(client, occupation, offset, 100)
                    except Exception as exc:
                        logger.warning("wikidata page failed for %s offset %s: %s", label, offset, exc)
                        break
                    if raw_count == 0:
                        logger.info("occupation %s exhausted at offset %s", label, offset)
                        break
                    offset += raw_count
                    fresh = [item for item in candidates if item.qid not in seen]
                    examined += len(fresh)
                    if not fresh:
                        await asyncio.sleep(0.4)
                        if raw_count < 100:
                            logger.info("occupation %s exhausted at offset %s", label, offset)
                            break
                        continue
                    remote = await fetch_metadata(client, fresh)
                    found_ids = {item.qid for item in remote}
                    for item in fresh:
                        if item.qid not in found_ids:
                            await remember_skip(pool, seen, item.qid, "no_image")
                            skipped += 1
                    downloaded = await download_many(client, remote)
                    for image, payload in downloaded:
                        if enrolled >= target:
                            break
                        if not payload:
                            await remember_skip(pool, seen, image.qid, "download_failed")
                            skipped += 1
                            continue
                        converted = to_jpeg(payload)
                        if converted is None:
                            await remember_skip(pool, seen, image.qid, "bad_image")
                            skipped += 1
                            continue
                        jpeg, width, height = converted
                        try:
                            detected = await asyncio.to_thread(engine.describe, jpeg)
                        except ImageRejected as exc:
                            await remember_skip(pool, seen, image.qid, exc.reason)
                            skipped += 1
                            continue
                        digest = hashlib.sha1(jpeg).hexdigest()
                        path = Path(settings.photo_dir) / f"{digest}.jpg"
                        if not path.exists():
                            await asyncio.to_thread(save_jpeg, path, jpeg)
                        inserted = await insert_face(
                            pool,
                            file_sha1=digest,
                            person_name=image.name,
                            wikidata_id=image.qid,
                            embedding=detected.embedding,
                            source_url=image.source_url,
                            author=image.author,
                            license_name=image.license_name,
                            license_url=image.license_url,
                            width=width,
                            height=height,
                        )
                        seen.add(image.qid)
                        if not inserted:
                            skipped += 1
                            continue
                        enrolled += 1
                        elapsed = time.perf_counter() - started
                        added = enrolled - initial
                        rate = added / elapsed if elapsed else 0
                        logger.info(
                            "enrolled=%s/%s skipped=%s person=%r sha1=%s rate=%.2f/s",
                            enrolled,
                            target,
                            skipped,
                            image.name,
                            digest,
                            rate,
                        )
                    await asyncio.sleep(0.4)
                    if raw_count < 100:
                        logger.info("occupation %s exhausted at offset %s", label, offset)
                        break

        logger.info("import finished with %s/%s faces", enrolled, target)
        return 0 if enrolled else 1
    finally:
        await pool.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enroll Wikimedia Commons portraits")
    parser.add_argument("--target", type=int, default=20000)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete enrolled rows and JPEGs, then import again.",
    )
    args = parser.parse_args(argv)
    if args.target < 1:
        parser.error("--target must be at least 1")
    return args


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    args = parse_args(sys.argv[1:] if argv is None else argv)
    raise SystemExit(asyncio.run(run(args.target, args.reset)))


if __name__ == "__main__":
    main()

import asyncio
import logging
import sqlite3
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
import numpy as np
import sqlite_vec

from app.config import settings

logger = logging.getLogger(__name__)

STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS faces (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_sha1 TEXT NOT NULL UNIQUE
            CHECK (length(file_sha1) = 40 AND file_sha1 NOT GLOB '*[^0-9a-f]*'),
        person_name TEXT NOT NULL,
        wikidata_id TEXT UNIQUE,
        embedding BLOB NOT NULL,
        source_url TEXT,
        author TEXT,
        license TEXT,
        license_url TEXT,
        width INTEGER,
        height INTEGER,
        created_at INTEGER NOT NULL DEFAULT (unixepoch())
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS searches (
        id TEXT PRIMARY KEY,
        embedding BLOB NOT NULL,
        faces_found INTEGER NOT NULL,
        box_x1 REAL,
        box_y1 REAL,
        box_x2 REAL,
        box_y2 REAL,
        created_at INTEGER NOT NULL DEFAULT (unixepoch())
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS import_skips (
        wikidata_id TEXT PRIMARY KEY,
        reason TEXT NOT NULL,
        created_at INTEGER NOT NULL DEFAULT (unixepoch())
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gallery_meta (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        model_name TEXT NOT NULL,
        embedding_dim INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS folder_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        root_path TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        face_index INTEGER NOT NULL DEFAULT 0,
        mtime_ns INTEGER NOT NULL,
        size_bytes INTEGER NOT NULL,
        embedding BLOB,
        skip_reason TEXT,
        UNIQUE (root_path, relative_path, face_index)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS folder_files_root_idx
    ON folder_files (root_path)
    """,
]


def pack_embedding(embedding) -> bytes:
    if isinstance(embedding, memoryview):
        embedding = embedding.tobytes()
    if isinstance(embedding, bytes):
        return embedding
    vector = np.ascontiguousarray(embedding, dtype=np.float32).reshape(-1)
    if vector.shape != (settings.embedding_dim,):
        raise RuntimeError(
            f"Expected a {settings.embedding_dim}-d embedding, got {vector.shape[0]}."
        )
    return sqlite_vec.serialize_float32(vector)


class Database:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()
        self._holding = False
        self._depth = 0

    async def close(self) -> None:
        await self._conn.close()

    async def _run(self, work):
        if self._holding:
            return await work()
        async with self._lock:
            self._holding = True
            try:
                return await work()
            finally:
                self._holding = False

    async def execute(self, sql: str, args: tuple = ()) -> None:
        async def work() -> None:
            await self._conn.execute(sql, args)
            if self._depth == 0:
                await self._conn.commit()

        await self._run(work)

    async def executemany(self, sql: str, args) -> None:
        async def work() -> None:
            await self._conn.executemany(sql, args)
            if self._depth == 0:
                await self._conn.commit()

        await self._run(work)

    async def fetch(self, sql: str, args: tuple = ()):
        async def work():
            cursor = await self._conn.execute(sql, args)
            rows = await cursor.fetchall()
            await cursor.close()
            return rows

        return await self._run(work)

    async def fetchrow(self, sql: str, args: tuple = (), *, commit: bool = False):
        async def work():
            cursor = await self._conn.execute(sql, args)
            row = await cursor.fetchone()
            await cursor.close()
            if commit and self._depth == 0:
                await self._conn.commit()
            return row

        return await self._run(work)

    async def fetchval(self, sql: str, args: tuple = ()):
        row = await self.fetchrow(sql, args)
        if row is None:
            return None
        return row[0]

    @asynccontextmanager
    async def transaction(self):
        async with self._lock:
            self._holding = True
            try:
                await self._conn.execute("BEGIN")
                self._depth += 1
                try:
                    yield
                    await self._conn.commit()
                except Exception:
                    await self._conn.rollback()
                    raise
                finally:
                    self._depth -= 1
            finally:
                self._holding = False


async def _ensure_model(db: Database) -> None:
    row = await db.fetchrow("SELECT model_name, embedding_dim FROM gallery_meta WHERE id = 1")
    if row is None:
        await db.execute(
            """
            INSERT INTO gallery_meta (id, model_name, embedding_dim)
            VALUES (1, ?, ?)
            """,
            (settings.model_name, settings.embedding_dim),
        )
        return
    if row["model_name"] == settings.model_name and row["embedding_dim"] == settings.embedding_dim:
        return
    count = await db.fetchval("SELECT count(*) FROM faces")
    if count:
        raise RuntimeError(
            "Gallery was enrolled with "
            f"{row['model_name']} ({row['embedding_dim']} dimensions) but this "
            f"service is {settings.model_name} ({settings.embedding_dim}). "
            "Reset the gallery before changing the recognition model."
        )
    await db.execute(
        """
        UPDATE gallery_meta
        SET model_name = ?, embedding_dim = ?
        WHERE id = 1
        """,
        (settings.model_name, settings.embedding_dim),
    )


async def migrate(db: Database) -> None:
    for statement in STATEMENTS:
        await db.execute(statement)
    await _ensure_model(db)


async def open_database(path: Path) -> Database:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path, isolation_level=None)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.execute("PRAGMA synchronous=NORMAL")
    try:
        await conn.enable_load_extension(True)
        await conn.load_extension(sqlite_vec.loadable_path())
    except (AttributeError, sqlite3.OperationalError) as exc:
        await conn.close()
        raise RuntimeError("This Python build cannot load sqlite-vec.") from exc
    db = Database(conn)
    await migrate(db)
    logger.info("database ready at %s", path)
    return db


async def open_pool() -> Database:
    return await open_database(Path(settings.database_path))


async def count_faces(pool: Database) -> int:
    return int(await pool.fetchval("SELECT count(*) FROM faces"))


async def load_seen_ids(pool: Database) -> set[str]:
    enrolled = await pool.fetch("SELECT wikidata_id FROM faces WHERE wikidata_id IS NOT NULL")
    skipped = await pool.fetch("SELECT wikidata_id FROM import_skips")
    return {row["wikidata_id"] for row in enrolled} | {row["wikidata_id"] for row in skipped}


async def insert_face(
    pool: Database,
    *,
    file_sha1: str,
    person_name: str,
    wikidata_id: str,
    embedding: np.ndarray,
    source_url: str | None,
    author: str | None,
    license_name: str | None,
    license_url: str | None,
    width: int,
    height: int,
) -> bool:
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO faces (
                file_sha1, person_name, wikidata_id, embedding,
                source_url, author, license, license_url, width, height
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (file_sha1) DO NOTHING
            RETURNING id
            """,
            (
                file_sha1,
                person_name,
                wikidata_id,
                pack_embedding(embedding),
                source_url,
                author,
                license_name,
                license_url,
                width,
                height,
            ),
            commit=True,
        )
    except sqlite3.IntegrityError:
        return False
    return row is not None


async def insert_skip(pool: Database, wikidata_id: str, reason: str) -> None:
    await pool.execute(
        """
        INSERT INTO import_skips (wikidata_id, reason)
        VALUES (?, ?)
        ON CONFLICT (wikidata_id) DO NOTHING
        """,
        (wikidata_id, reason),
    )


async def reset_gallery(pool: Database) -> None:
    async with pool.transaction():
        await pool.execute("DELETE FROM faces")
        await pool.execute("DELETE FROM searches")
        await pool.execute("DELETE FROM import_skips")


async def create_search(
    pool: Database,
    embedding: np.ndarray,
    faces_found: int,
    box: tuple[float, float, float, float],
) -> uuid.UUID:
    search_id = uuid.uuid4()
    await pool.execute("DELETE FROM searches WHERE created_at < unixepoch('now', '-1 day')")
    await pool.execute(
        """
        INSERT INTO searches (
            id, embedding, faces_found, box_x1, box_y1, box_x2, box_y2
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(search_id),
            pack_embedding(embedding),
            faces_found,
            box[0],
            box[1],
            box[2],
            box[3],
        ),
    )
    return search_id


async def get_search(pool: Database, search_id: uuid.UUID):
    return await pool.fetchrow(
        """
        SELECT embedding, faces_found, box_x1, box_y1, box_x2, box_y2
        FROM searches
        WHERE id = ?
        """,
        (str(search_id),),
    )


async def page_matches(
    pool: Database,
    embedding,
    min_similarity: float,
    page: int,
    page_size: int,
) -> tuple[int, list]:
    blob = pack_embedding(embedding)
    offset = (page - 1) * page_size
    total = int(
        await pool.fetchval(
            """
            SELECT count(*)
            FROM faces
            WHERE (1.0 - vec_distance_cosine(embedding, ?)) >= ?
            """,
            (blob, min_similarity),
        )
    )
    rows = await pool.fetch(
        """
        SELECT
            id,
            person_name,
            file_sha1,
            wikidata_id,
            source_url,
            author,
            license,
            license_url,
            similarity
        FROM (
            SELECT
                id,
                person_name,
                file_sha1,
                wikidata_id,
                source_url,
                author,
                license,
                license_url,
                (1.0 - vec_distance_cosine(embedding, ?)) AS similarity
            FROM faces
        ) scored
        WHERE similarity >= ?
        ORDER BY similarity DESC, id
        LIMIT ? OFFSET ?
        """,
        (blob, min_similarity, page_size, offset),
    )
    return total, list(rows)


async def sample_faces(pool: Database, limit: int) -> list:
    rows = await pool.fetch(
        """
        SELECT person_name, file_sha1
        FROM faces
        ORDER BY random()
        LIMIT ?
        """,
        (limit,),
    )
    return list(rows)


async def count_indexed_folder_files(pool: Database, root_path: str) -> int:
    return int(
        await pool.fetchval(
            """
            SELECT count(DISTINCT relative_path)
            FROM folder_files
            WHERE root_path = ? AND embedding IS NOT NULL
            """,
            (root_path,),
        )
    )


async def folder_file_meta(pool: Database, root_path: str) -> dict[str, tuple[int, int]]:
    """relative path -> (mtime_ns, size_bytes) for files already indexed."""
    rows = await pool.fetch(
        """
        SELECT relative_path, mtime_ns, size_bytes
        FROM folder_files
        WHERE root_path = ?
        """,
        (root_path,),
    )
    return {row["relative_path"]: (int(row["mtime_ns"]), int(row["size_bytes"])) for row in rows}


async def gallery_embeddings(pool: Database, sha1s: list[str]) -> dict[str, np.ndarray]:
    found: dict[str, np.ndarray] = {}
    if not sha1s:
        return found
    for start in range(0, len(sha1s), 500):
        chunk = sha1s[start : start + 500]
        marks = ",".join("?" for _ in chunk)
        rows = await pool.fetch(
            f"""
            SELECT file_sha1, embedding
            FROM faces
            WHERE file_sha1 IN ({marks})
            """,
            tuple(chunk),
        )
        for row in rows:
            found[row["file_sha1"]] = np.frombuffer(row["embedding"], dtype=np.float32).copy()
    return found


async def replace_folder_file(
    pool: Database,
    *,
    root_path: str,
    relative_path: str,
    mtime_ns: int,
    size_bytes: int,
    embeddings: list[np.ndarray],
    skip_reason: str | None,
) -> None:
    async with pool.transaction():
        await pool.execute(
            """
            DELETE FROM folder_files
            WHERE root_path = ? AND relative_path = ?
            """,
            (root_path, relative_path),
        )
        if skip_reason or not embeddings:
            await pool.execute(
                """
                INSERT INTO folder_files (
                    root_path, relative_path, face_index, mtime_ns, size_bytes, skip_reason
                )
                VALUES (?, ?, 0, ?, ?, ?)
                """,
                (root_path, relative_path, mtime_ns, size_bytes, skip_reason or "no_face"),
            )
            return
        for index, embedding in enumerate(embeddings):
            await pool.execute(
                """
                INSERT INTO folder_files (
                    root_path, relative_path, face_index, mtime_ns, size_bytes, embedding
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    root_path,
                    relative_path,
                    index,
                    mtime_ns,
                    size_bytes,
                    pack_embedding(embedding),
                ),
            )


async def drop_missing_folder_files(
    pool: Database,
    root_path: str,
    relative_paths: list[str],
) -> None:
    async with pool.transaction():
        await pool.execute("CREATE TEMP TABLE IF NOT EXISTS keep_paths (path TEXT PRIMARY KEY)")
        await pool.execute("DELETE FROM keep_paths")
        if not relative_paths:
            await pool.execute("DELETE FROM folder_files WHERE root_path = ?", (root_path,))
            return
        for start in range(0, len(relative_paths), 500):
            chunk = relative_paths[start : start + 500]
            await pool.executemany(
                "INSERT OR IGNORE INTO keep_paths (path) VALUES (?)",
                [(path,) for path in chunk],
            )
        await pool.execute(
            """
            DELETE FROM folder_files
            WHERE root_path = ?
              AND relative_path NOT IN (SELECT path FROM keep_paths)
            """,
            (root_path,),
        )


async def search_folder_faces(
    pool: Database,
    root_path: str,
    embedding,
    min_similarity: float,
    limit: int,
) -> tuple[int, list]:
    blob = pack_embedding(embedding)
    total = int(
        await pool.fetchval(
            """
            SELECT count(*)
            FROM (
                SELECT relative_path
                FROM folder_files
                WHERE root_path = ? AND embedding IS NOT NULL
                GROUP BY relative_path
                HAVING MAX(1.0 - vec_distance_cosine(embedding, ?)) >= ?
            )
            """,
            (root_path, blob, min_similarity),
        )
    )
    rows = await pool.fetch(
        """
        SELECT relative_path, similarity
        FROM (
            SELECT
                relative_path,
                MAX(1.0 - vec_distance_cosine(embedding, ?)) AS similarity
            FROM folder_files
            WHERE root_path = ? AND embedding IS NOT NULL
            GROUP BY relative_path
        )
        WHERE similarity >= ?
        ORDER BY similarity DESC, relative_path
        LIMIT ?
        """,
        (blob, root_path, min_similarity, limit),
    )
    return total, list(rows)

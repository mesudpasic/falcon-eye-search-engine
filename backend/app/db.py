import asyncio
import logging
import uuid

import asyncpg
import numpy as np
from pgvector.asyncpg import register_vector

from app.config import settings

logger = logging.getLogger(__name__)

LOCK_KEY = 804251

STATEMENTS = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    """
    CREATE TABLE IF NOT EXISTS faces (
        id BIGSERIAL PRIMARY KEY,
        file_sha1 TEXT NOT NULL UNIQUE CHECK (file_sha1 ~ '^[0-9a-f]{40}$'),
        person_name TEXT NOT NULL,
        wikidata_id TEXT UNIQUE,
        embedding vector(512) NOT NULL,
        source_url TEXT,
        author TEXT,
        license TEXT,
        license_url TEXT,
        width INTEGER,
        height INTEGER,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS searches (
        id UUID PRIMARY KEY,
        embedding vector(512) NOT NULL,
        faces_found INTEGER NOT NULL,
        box_x1 REAL,
        box_y1 REAL,
        box_x2 REAL,
        box_y2 REAL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS import_skips (
        wikidata_id TEXT PRIMARY KEY,
        reason TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
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
        id BIGSERIAL PRIMARY KEY,
        root_path TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        face_index INTEGER NOT NULL DEFAULT 0,
        mtime_ns BIGINT NOT NULL,
        size_bytes BIGINT NOT NULL,
        embedding vector(512),
        skip_reason TEXT,
        UNIQUE (root_path, relative_path, face_index)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS folder_files_root_idx
    ON folder_files (root_path)
    """,
]

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS faces_embedding_hnsw
ON faces USING hnsw (embedding vector_cosine_ops)
"""


async def _register(conn: asyncpg.Connection) -> None:
    await register_vector(conn)


async def _ensure_model(conn: asyncpg.Connection) -> None:
    row = await conn.fetchrow(
        "SELECT model_name, embedding_dim FROM gallery_meta WHERE id = 1"
    )
    if row is None:
        await conn.execute(
            """
            INSERT INTO gallery_meta (id, model_name, embedding_dim)
            VALUES (1, $1, $2)
            """,
            settings.model_name,
            settings.embedding_dim,
        )
        return
    if (
        row["model_name"] == settings.model_name
        and row["embedding_dim"] == settings.embedding_dim
    ):
        return
    count = await conn.fetchval("SELECT count(*) FROM faces")
    if count:
        raise RuntimeError(
            "Gallery was enrolled with "
            f"{row['model_name']} ({row['embedding_dim']} dimensions) but this "
            f"service is {settings.model_name} ({settings.embedding_dim}). "
            "Reset the gallery before changing the recognition model."
        )
    await conn.execute(
        """
        UPDATE gallery_meta
        SET model_name = $1, embedding_dim = $2
        WHERE id = 1
        """,
        settings.model_name,
        settings.embedding_dim,
    )


async def migrate(conn: asyncpg.Connection) -> None:
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", LOCK_KEY)
        for statement in STATEMENTS:
            await conn.execute(statement)
        try:
            async with conn.transaction():
                await conn.execute(INDEX_SQL)
        except asyncpg.PostgresError as exc:
            logger.warning("similarity index was not created: %s", exc)
        await _ensure_model(conn)


async def open_pool() -> asyncpg.Pool:
    last_error: Exception | None = None
    for attempt in range(1, 31):
        try:
            conn = await asyncpg.connect(
                settings.database_url,
                server_settings={"application_name": "falcon-eye-engine-migrate"},
            )
            try:
                await migrate(conn)
            finally:
                await conn.close()
            logger.info("database ready")
            return await asyncpg.create_pool(
                settings.database_url,
                min_size=1,
                max_size=8,
                init=_register,
                server_settings={"application_name": "falcon-eye-engine"},
            )
        except RuntimeError:
            raise
        except Exception as exc:
            last_error = exc
            logger.warning("database not ready (attempt %s): %s", attempt, exc)
            await asyncio.sleep(2)
    raise RuntimeError("could not connect to the database") from last_error


async def count_faces(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        return int(await conn.fetchval("SELECT count(*) FROM faces"))


async def load_seen_ids(pool: asyncpg.Pool) -> set[str]:
    async with pool.acquire() as conn:
        enrolled = await conn.fetch(
            "SELECT wikidata_id FROM faces WHERE wikidata_id IS NOT NULL"
        )
        skipped = await conn.fetch("SELECT wikidata_id FROM import_skips")
    return {row["wikidata_id"] for row in enrolled} | {row["wikidata_id"] for row in skipped}


async def insert_face(
    pool: asyncpg.Pool,
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
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO faces (
                    file_sha1, person_name, wikidata_id, embedding,
                    source_url, author, license, license_url, width, height
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (file_sha1) DO NOTHING
                RETURNING id
                """,
                file_sha1,
                person_name,
                wikidata_id,
                embedding,
                source_url,
                author,
                license_name,
                license_url,
                width,
                height,
            )
        except asyncpg.UniqueViolationError:
            return False
    return row is not None


async def insert_skip(pool: asyncpg.Pool, wikidata_id: str, reason: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO import_skips (wikidata_id, reason)
            VALUES ($1, $2)
            ON CONFLICT (wikidata_id) DO NOTHING
            """,
            wikidata_id,
            reason,
        )


async def reset_gallery(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE faces, searches, import_skips")


async def create_search(
    pool: asyncpg.Pool,
    embedding: np.ndarray,
    faces_found: int,
    box: tuple[float, float, float, float],
) -> uuid.UUID:
    search_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM searches WHERE created_at < now() - interval '1 day'"
        )
        await conn.execute(
            """
            INSERT INTO searches (
                id, embedding, faces_found, box_x1, box_y1, box_x2, box_y2
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            search_id,
            embedding,
            faces_found,
            box[0],
            box[1],
            box[2],
            box[3],
        )
    return search_id


async def get_search(pool: asyncpg.Pool, search_id: uuid.UUID) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT embedding, faces_found, box_x1, box_y1, box_x2, box_y2
            FROM searches
            WHERE id = $1
            """,
            search_id,
        )


async def page_matches(
    pool: asyncpg.Pool,
    embedding: np.ndarray,
    min_similarity: float,
    page: int,
    page_size: int,
) -> tuple[int, list[asyncpg.Record]]:
    offset = (page - 1) * page_size
    async with pool.acquire() as conn:
        total = int(
            await conn.fetchval(
                """
                SELECT count(*)
                FROM faces
                WHERE (1 - (embedding <=> $1)) >= $2
                """,
                embedding,
                min_similarity,
            )
        )
        rows = await conn.fetch(
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
                (1 - (embedding <=> $1)) AS similarity
            FROM faces
            WHERE (1 - (embedding <=> $1)) >= $2
            ORDER BY (embedding <=> $1), id
            LIMIT $3 OFFSET $4
            """,
            embedding,
            min_similarity,
            page_size,
            offset,
        )
    return total, list(rows)


async def sample_faces(pool: asyncpg.Pool, limit: int) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT person_name, file_sha1
            FROM faces
            ORDER BY random()
            LIMIT $1
            """,
            limit,
        )
    return list(rows)


async def count_indexed_folder_files(pool: asyncpg.Pool, root_path: str) -> int:
    async with pool.acquire() as conn:
        return int(
            await conn.fetchval(
                """
                SELECT count(DISTINCT relative_path)
                FROM folder_files
                WHERE root_path = $1 AND embedding IS NOT NULL
                """,
                root_path,
            )
        )


async def folder_file_meta(pool: asyncpg.Pool, root_path: str) -> dict[str, tuple[int, int]]:
    """relative path -> (mtime_ns, size_bytes) for files already indexed."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT relative_path, mtime_ns, size_bytes
            FROM folder_files
            WHERE root_path = $1
            """,
            root_path,
        )
    return {row["relative_path"]: (int(row["mtime_ns"]), int(row["size_bytes"])) for row in rows}


async def gallery_embeddings(pool: asyncpg.Pool, sha1s: list[str]) -> dict[str, np.ndarray]:
    found: dict[str, np.ndarray] = {}
    if not sha1s:
        return found
    async with pool.acquire() as conn:
        for start in range(0, len(sha1s), 500):
            chunk = sha1s[start : start + 500]
            rows = await conn.fetch(
                """
                SELECT file_sha1, embedding
                FROM faces
                WHERE file_sha1 = ANY($1::text[])
                """,
                chunk,
            )
            for row in rows:
                found[row["file_sha1"]] = np.ascontiguousarray(row["embedding"], dtype=np.float32)
    return found


async def replace_folder_file(
    pool: asyncpg.Pool,
    *,
    root_path: str,
    relative_path: str,
    mtime_ns: int,
    size_bytes: int,
    embeddings: list[np.ndarray],
    skip_reason: str | None,
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                DELETE FROM folder_files
                WHERE root_path = $1 AND relative_path = $2
                """,
                root_path,
                relative_path,
            )
            if skip_reason or not embeddings:
                await conn.execute(
                    """
                    INSERT INTO folder_files (
                        root_path, relative_path, face_index, mtime_ns, size_bytes, skip_reason
                    )
                    VALUES ($1, $2, 0, $3, $4, $5)
                    """,
                    root_path,
                    relative_path,
                    mtime_ns,
                    size_bytes,
                    skip_reason or "no_face",
                )
                return
            for index, embedding in enumerate(embeddings):
                await conn.execute(
                    """
                    INSERT INTO folder_files (
                        root_path, relative_path, face_index, mtime_ns, size_bytes, embedding
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    root_path,
                    relative_path,
                    index,
                    mtime_ns,
                    size_bytes,
                    np.ascontiguousarray(embedding, dtype=np.float32),
                )


async def drop_missing_folder_files(
    pool: asyncpg.Pool,
    root_path: str,
    relative_paths: list[str],
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DELETE FROM folder_files
            WHERE root_path = $1
              AND NOT (relative_path = ANY($2::text[]))
            """,
            root_path,
            relative_paths,
        )


async def search_folder_faces(
    pool: asyncpg.Pool,
    root_path: str,
    embedding: np.ndarray,
    min_similarity: float,
    limit: int,
) -> tuple[int, list[asyncpg.Record]]:
    vector = np.ascontiguousarray(embedding, dtype=np.float32)
    async with pool.acquire() as conn:
        total = int(
            await conn.fetchval(
                """
                SELECT count(DISTINCT relative_path)
                FROM folder_files
                WHERE root_path = $1
                  AND embedding IS NOT NULL
                  AND (1 - (embedding <=> $2)) >= $3
                """,
                root_path,
                vector,
                min_similarity,
            )
        )
        rows = await conn.fetch(
            """
            SELECT relative_path, similarity
            FROM (
                SELECT DISTINCT ON (relative_path)
                    relative_path,
                    (1 - (embedding <=> $2)) AS similarity
                FROM folder_files
                WHERE root_path = $1
                  AND embedding IS NOT NULL
                  AND (1 - (embedding <=> $2)) >= $3
                ORDER BY relative_path, (embedding <=> $2)
            ) matches
            ORDER BY similarity DESC, relative_path
            LIMIT $4
            """,
            root_path,
            vector,
            min_similarity,
            limit,
        )
    return total, list(rows)

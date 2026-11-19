from uuid import UUID

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    enrolled_faces: int
    model_name: str


class StatsResponse(BaseModel):
    enrolled_faces: int
    model_name: str
    embedding_dim: int
    det_size: int


class SampleResponse(BaseModel):
    person_name: str
    file_sha1: str
    photo_url: str


class MatchResponse(BaseModel):
    person_name: str
    file_sha1: str = Field(description="SHA-1 of the stored JPEG. This is the file name.")
    similarity: float = Field(description="Cosine similarity of the two face embeddings, from 0 to 1.")
    match_percent: float = Field(description="similarity × 100, rounded to one decimal.")
    photo_url: str
    source_url: str | None = None
    author: str | None = None
    license: str | None = None
    license_url: str | None = None
    wikidata_url: str | None = None


class SearchResponse(BaseModel):
    search_id: UUID
    page: int
    page_size: int
    total: int = Field(description="How many gallery faces are at or above min_similarity.")
    pages: int
    has_previous: bool
    has_next: bool
    min_similarity: float
    faces_found: int
    face_box: list[float] | None = Field(
        default=None,
        description="Detected face as fractions of the image width and height: x1, y1, x2, y2.",
    )
    results: list[MatchResponse]


class DirectoryEntry(BaseModel):
    name: str
    path: str


class FolderListing(BaseModel):
    path: str
    parent: str | None = None
    directories: list[DirectoryEntry]
    image_count: int
    truncated: bool = False


class FolderUploadResponse(BaseModel):
    path: str
    image_count: int


class FolderMatch(BaseModel):
    name: str
    relative_path: str
    photo_url: str
    similarity: float
    match_percent: float


class FolderJobResult(BaseModel):
    folder: str
    examined: int
    with_faces: int
    skipped: int
    face_box: list[float] | None = None
    total: int
    results: list[FolderMatch]


class TimeHit(BaseModel):
    label: str
    start_seconds: float
    end_seconds: float
    start_label: str
    end_label: str
    score: float
    match_percent: float
    snapshot_url: str


class ScanResult(BaseModel):
    duration_seconds: float
    frames_checked: int
    sample_fps: float
    query: str | None = None
    note: str = ""
    hits: list[TimeHit]


class GalleryBuildResult(BaseModel):
    folder: str
    examined: int
    enrolled: int
    skipped: int


class JobResponse(BaseModel):
    job_id: UUID
    kind: str
    status: str
    progress: float
    message: str
    error: str | None = None
    folder: FolderJobResult | None = None
    scan: ScanResult | None = None
    gallery: GalleryBuildResult | None = None

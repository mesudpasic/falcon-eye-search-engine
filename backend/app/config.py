from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    database_path: str = "/data/faces.db"
    data_dir: str = "/data"
    photo_dir: str = "/data/photos"
    insightface_root: str = "/models"
    model_name: str = "buffalo_l"
    det_size: int = 640
    embedding_dim: int = 512
    max_image_edge: int = 1600
    max_upload_bytes: int = 10 * 1024 * 1024
    max_folder_image_bytes: int = 25 * 1024 * 1024
    max_folder_files: int = 30000
    max_video_bytes: int = 2 * 1024 * 1024 * 1024
    video_sample_fps: float = 2.0
    default_min_similarity: float = 0.35
    onnx_providers: str = "CPUExecutionProvider"
    cors_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:8080,http://127.0.0.1:8080,"
        "http://localhost:9080,http://127.0.0.1:9080"
    )

    @property
    def provider_list(self) -> list[str]:
        return [item.strip() for item in self.onnx_providers.split(",") if item.strip()]

    @property
    def origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


settings = Settings()

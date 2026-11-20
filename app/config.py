"""Environment settings with defaults. Read once at import time."""

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"invalid integer for {name}")


BACKENDS = ("faster-whisper", "mlx")


def _backend() -> str:
    raw = os.environ.get("WHISPER_BACKEND", "").strip().lower()
    if not raw:
        return BACKENDS[0]
    if raw not in BACKENDS:
        raise SystemExit("WHISPER_BACKEND must be one of: " + ", ".join(BACKENDS))
    return raw


@dataclass(frozen=True)
class Settings:
    data_dir: str
    model_name: str
    compute_type: str
    device: str
    cpu_threads: int
    retention_hours: int
    max_upload_bytes: int
    max_media_seconds: int
    port: int
    hf_home: str
    whisper_backend: str = "faster-whisper"
    mlx_model: str = "mlx-community/whisper-large-v3-turbo"

    @property
    def jobs_dir(self) -> str:
        return os.path.join(self.data_dir, "jobs")

    @property
    def retention_seconds(self) -> int:
        return self.retention_hours * 3600


def load() -> Settings:
    return Settings(
        data_dir=os.environ.get("DATA_DIR", "/data"),
        model_name=os.environ.get("MODEL_NAME", "large-v3-turbo"),
        compute_type=os.environ.get("COMPUTE_TYPE", "int8"),
        device=os.environ.get("DEVICE", "cpu"),
        cpu_threads=_int("CPU_THREADS", 4),
        retention_hours=_int("RETENTION_HOURS", 24),
        max_upload_bytes=_int("MAX_UPLOAD_BYTES", 2147483648),
        max_media_seconds=_int("MAX_MEDIA_SECONDS", 21600),
        port=_int("PORT", 8090),
        hf_home=os.environ.get("HF_HOME", "/models"),
        whisper_backend=_backend(),
        mlx_model=os.environ.get("MLX_MODEL", "mlx-community/whisper-large-v3-turbo"),
    )


settings = load()

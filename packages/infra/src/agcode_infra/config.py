from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[4]


@dataclass(frozen=True)
class DatabaseSettings:
    sql_type: str
    sql_user: str
    sql_password: str
    sql_host: str
    sql_port: str
    sql_db: str

    @property
    def url(self) -> str:
        return (
            f"{self.sql_type}://{self.sql_user}:{self.sql_password}"
            f"@{self.sql_host}:{self.sql_port}/{self.sql_db}"
        )


@dataclass(frozen=True)
class RedisSettings:
    url: str


@dataclass(frozen=True)
class SessionRuntimeSettings:
    runtime_mode: str
    image_name_coder_pro: str | None
    image_name_coder_noob: str | None
    local_image_name_coder_pro: str | None
    local_image_name_coder_noob: str | None
    worker_build_id: str
    namespace: str
    storage_class_name: str
    pvc_size: str
    scheduling_timeout_seconds: int
    worker_port: int
    worker_socketio_path: str
    remote_config_path: Path
    noob_runtime_class_name: str | None
    noob_mount_path: str


def get_database_settings() -> DatabaseSettings:
    return DatabaseSettings(
        sql_type=os.getenv("SQL_TYPE", ""),
        sql_user=os.getenv("SQL_USER", ""),
        sql_password=os.getenv("SQL_PASSWORD", ""),
        sql_host=os.getenv("SQL_HOST", ""),
        sql_port=os.getenv("SQL_PORT", ""),
        sql_db=os.getenv("SQL_DB", ""),
    )


def get_redis_settings() -> RedisSettings:
    return RedisSettings(
        url=os.getenv("REDIS_URL", "redis://localhost:6379"),
    )


def get_session_runtime_settings() -> SessionRuntimeSettings:
    default_remote_config = _project_root() / "deploy" / "k8s" / "remote-config.yaml"
    return SessionRuntimeSettings(
        runtime_mode=os.getenv("SESSION_RUNTIME_MODE", "remote"),
        image_name_coder_pro=os.getenv("IMAGE_NAME_CODER_PRO"),
        image_name_coder_noob=os.getenv("IMAGE_NAME_CODER_NOOB"),
        local_image_name_coder_pro=os.getenv("LOCAL_IMAGE_NAME_CODER_PRO"),
        local_image_name_coder_noob=os.getenv("LOCAL_IMAGE_NAME_CODER_NOOB"),
        worker_build_id=os.getenv("WORKER_BUILD_ID", ""),
        namespace=os.getenv("SESSION_K8S_NAMESPACE", "default"),
        storage_class_name=os.getenv("SESSION_K8S_STORAGE_CLASS", "microk8s-hostpath"),
        pvc_size=os.getenv("SESSION_K8S_PVC_SIZE", "1Gi"),
        scheduling_timeout_seconds=int(os.getenv("SESSION_SCHEDULING_TIMEOUT_SECONDS", "30")),
        worker_port=int(os.getenv("SESSION_WORKER_PORT", "8000")),
        worker_socketio_path=os.getenv("SESSION_WORKER_SOCKETIO_PATH", "/chat/realtime"),
        remote_config_path=Path(os.getenv("SESSION_REMOTE_CONFIG_PATH", str(default_remote_config))),
        noob_runtime_class_name=os.getenv("SESSION_NOOB_RUNTIME_CLASS", "runsc"),
        noob_mount_path=os.getenv("SESSION_NOOB_MOUNT_PATH", "/mnt/session"),
    )

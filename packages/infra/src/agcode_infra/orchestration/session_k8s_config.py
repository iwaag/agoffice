import os
import re

from agcode_infra.config import get_session_runtime_settings

SETTINGS = get_session_runtime_settings()
RUNTIME_MODE = SETTINGS.runtime_mode
IMAGE_NAME_CODER_PRO = SETTINGS.image_name_coder_pro
IMAGE_NAME_CODER_NOOB = SETTINGS.image_name_coder_noob
IMAGE_NAME_CODER_NOOB_PREP = SETTINGS.image_name_coder_noob_prep
LOCAL_IMAGE_NAME_CODER_PRO = SETTINGS.local_image_name_coder_pro
LOCAL_IMAGE_NAME_CODER_NOOB = SETTINGS.local_image_name_coder_noob
LOCAL_IMAGE_NAME_CODER_NOOB_PREP = SETTINGS.local_image_name_coder_noob_prep
WORKER_BUILD_ID = SETTINGS.worker_build_id
NAMESPACE = SETTINGS.namespace
STORAGE_CLASS_NAME = SETTINGS.storage_class_name
PVC_SIZE = SETTINGS.pvc_size
SCHEDULING_TIMEOUT_SECONDS = SETTINGS.scheduling_timeout_seconds
WORKER_PORT = SETTINGS.worker_port
WORKER_SOCKETIO_PATH = SETTINGS.worker_socketio_path
REMOTE_CONFIG_PATH = SETTINGS.remote_config_path
NOOB_RUNTIME_CLASS_NAME = SETTINGS.noob_runtime_class_name
PRO_MOUNT_PATH = SETTINGS.pro_mount_path
NOOB_MOUNT_PATH = SETTINGS.noob_mount_path

HATCHET_CLIENT_TOKEN = os.getenv("HATCHET_CLIENT_TOKEN")
HATCHET_CLIENT_HOST_PORT = os.getenv("HATCHET_CLIENT_HOST_PORT")
HATCHET_CLIENT_SERVER_URL = os.getenv("HATCHET_CLIENT_SERVER_URL")
HATCHET_CLIENT_TLS_STRATEGY = os.getenv("HATCHET_CLIENT_TLS_STRATEGY", "none")
CLIENT_ID = os.getenv("CLIENT_ID", "agcode")
KEYCLOAK_URL = os.getenv("KEYCLOAK_URL", "http://localhost:8080")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM", "agcode")
KEYCLOAK_CLIENT_SECRET = os.getenv("KEYCLOAK_CLIENT_SECRET")

NOOB_REQUEST_PATH = f"{NOOB_MOUNT_PATH}/control/request.json"
NOOB_STATUS_PATH = f"{NOOB_MOUNT_PATH}/state/status.json"
NOOB_RESULT_PATH = f"{NOOB_MOUNT_PATH}/state/result.json"
NOOB_EVENTS_PATH = f"{NOOB_MOUNT_PATH}/events/events.jsonl"


def is_local_microk8s_mode() -> bool:
    return RUNTIME_MODE == "local_microk8s"


def to_k8s_name_fragment(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not normalized:
        raise ValueError("Kubernetes resource name fragment cannot be empty")
    return normalized


def resolve_image(image_name: str | None, env_name: str) -> str:
    if not image_name:
        raise ValueError(f"{env_name} is not set")
    if "@" in image_name:
        return image_name

    last_segment = image_name.rsplit("/", 1)[-1]
    if ":" in last_segment:
        return image_name

    return f"{image_name}:latest"


def get_coder_pro_image() -> str:
    if is_local_microk8s_mode():
        return resolve_image(LOCAL_IMAGE_NAME_CODER_PRO, "LOCAL_IMAGE_NAME_CODER_PRO")
    return resolve_image(IMAGE_NAME_CODER_PRO, "IMAGE_NAME_CODER_PRO")


def get_coder_noob_image() -> str:
    if is_local_microk8s_mode():
        return resolve_image(LOCAL_IMAGE_NAME_CODER_NOOB, "LOCAL_IMAGE_NAME_CODER_NOOB")
    return resolve_image(IMAGE_NAME_CODER_NOOB, "IMAGE_NAME_CODER_NOOB")


def get_coder_noob_prep_image() -> str:
    if is_local_microk8s_mode():
        return resolve_image(LOCAL_IMAGE_NAME_CODER_NOOB_PREP, "LOCAL_IMAGE_NAME_CODER_NOOB_PREP")
    return resolve_image(IMAGE_NAME_CODER_NOOB_PREP, "IMAGE_NAME_CODER_NOOB_PREP")


def get_image_pull_policy() -> str | None:
    if is_local_microk8s_mode():
        return "Never"
    return None


def session_resource_names(session_id: str) -> dict[str, str]:
    task_name = to_k8s_name_fragment(session_id)
    return {
        "pro_pvc_name": f"pvc-session-{task_name}-pro",
        "pro_pod_name": f"worker-session-{task_name}-pro",
        "pro_service_name": f"svc-session-{task_name}-pro",
        "noob_pvc_name": f"pvc-session-{task_name}-noob",
        "noob_pod_name": f"worker-session-{task_name}-noob",
        "noob_prep_job_name": f"workspace-prep-{task_name}-noob",
    }


def get_pro_service_name(session_id: str) -> str:
    return session_resource_names(session_id)["pro_service_name"]


def get_noob_pod_name(session_id: str) -> str:
    return session_resource_names(session_id)["noob_pod_name"]


def get_noob_prep_job_name(session_id: str) -> str:
    return session_resource_names(session_id)["noob_prep_job_name"]

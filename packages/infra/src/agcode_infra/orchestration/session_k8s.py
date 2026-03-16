import os
import re
import time

from kubernetes import client, config
from kubernetes.client.rest import ApiException

from agcode_domain.schema import SessionInfo
from agcode_infra.config import get_session_runtime_settings
from agcode_infra.db import database as db

SETTINGS = get_session_runtime_settings()
IMAGE_NAME_CODER_PRO = SETTINGS.image_name_coder_pro
NAMESPACE = SETTINGS.namespace
STORAGE_CLASS_NAME = SETTINGS.storage_class_name
PVC_SIZE = SETTINGS.pvc_size
SCHEDULING_TIMEOUT_SECONDS = SETTINGS.scheduling_timeout_seconds
WORKER_PORT = SETTINGS.worker_port
WORKER_SOCKETIO_PATH = SETTINGS.worker_socketio_path
REMOTE_CONFIG_PATH = SETTINGS.remote_config_path


def _to_k8s_name_fragment(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not normalized:
        raise ValueError("Kubernetes resource name fragment cannot be empty")
    return normalized


def _resolve_image(image_name: str | None, env_name: str) -> str:
    if not image_name:
        raise ValueError(f"{env_name} is not set")
    if "@" in image_name:
        return image_name

    last_segment = image_name.rsplit("/", 1)[-1]
    if ":" in last_segment:
        return image_name

    return f"{image_name}:latest"


def _session_resource_names(session_id: str) -> dict[str, str]:
    task_name = _to_k8s_name_fragment(session_id)
    return {
        "pro_pvc_name": f"pvc-session-{task_name}-pro",
        "pro_pod_name": f"worker-session-{task_name}-pro",
        "pro_service_name": f"svc-session-{task_name}-pro",
    }


def _ensure_pvc(v1: client.CoreV1Api, pvc_name: str) -> None:
    try:
        v1.read_namespaced_persistent_volume_claim(name=pvc_name, namespace=NAMESPACE)
        print(f"PVC {pvc_name} already exists. Reusing...")
        return
    except ApiException as e:
        if e.status != 404:
            raise

    print(f"PVC {pvc_name} not found. Creating new one...")
    pvc_body = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(name=pvc_name),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            resources=client.V1ResourceRequirements(requests={"storage": PVC_SIZE}),
            storage_class_name=STORAGE_CLASS_NAME,
        ),
    )
    v1.create_namespaced_persistent_volume_claim(namespace=NAMESPACE, body=pvc_body)


def _ensure_service(
    v1: client.CoreV1Api,
    *,
    service_name: str,
    selector: dict[str, str],
    port: int = WORKER_PORT,
) -> None:
    try:
        v1.read_namespaced_service(name=service_name, namespace=NAMESPACE)
        print(f"Service {service_name} already exists. Reusing...")
        return
    except ApiException as e:
        if e.status != 404:
            raise

    print(f"Service {service_name} not found. Creating new one...")
    service_body = client.V1Service(
        metadata=client.V1ObjectMeta(name=service_name),
        spec=client.V1ServiceSpec(
            selector=selector,
            ports=[
                client.V1ServicePort(
                    name="ws",
                    port=port,
                    target_port=port,
                    protocol="TCP",
                )
            ],
            type="ClusterIP",
        ),
    )
    v1.create_namespaced_service(namespace=NAMESPACE, body=service_body)


def _build_pod(
    *,
    pod_name: str,
    session_id: str,
    user_id: str,
    role: str,
    image: str,
    own_pvc_name: str,
    peer_pvc_name: str | None = None,
    node_name: str | None = None,
) -> client.V1Pod:
    labels = {
        "task-id": session_id,
        "user-id": user_id,
        "type": "session-worker",
        "role": role,
    }
    volume_mounts = [
        client.V1VolumeMount(
            name="own-task-data",
            mount_path="/mnt/data",
            read_only=False,
        ),
    ]
    volumes = [
        client.V1Volume(
            name="own-task-data",
            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                claim_name=own_pvc_name,
                read_only=False,
            ),
        ),
    ]
    if peer_pvc_name is not None:
        volume_mounts.append(
            client.V1VolumeMount(
                name="peer-task-data",
                mount_path="/mnt/peer-data",
                read_only=True,
            )
        )
        volumes.append(
            client.V1Volume(
                name="peer-task-data",
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                    claim_name=peer_pvc_name,
                    read_only=True,
                ),
            )
        )

    return client.V1Pod(
        metadata=client.V1ObjectMeta(name=pod_name, labels=labels),
        spec=client.V1PodSpec(
            restart_policy="Never",
            node_name=node_name,
            containers=[
                client.V1Container(
                    name=f"{role}-container",
                    image=image,
                    volume_mounts=volume_mounts,
                    env=[
                        client.V1EnvVar(name="TASK_ID", value=session_id),
                        client.V1EnvVar(name="SESSION_ROLE", value=role),
                    ],
                )
            ],
            volumes=volumes,
        ),
    )


def _create_or_reuse_pod(v1: client.CoreV1Api, pod_spec: client.V1Pod) -> client.V1Pod:
    pod_name = pod_spec.metadata.name
    try:
        pod = v1.create_namespaced_pod(namespace=NAMESPACE, body=pod_spec)
        print(f"Pod {pod_name} created successfully.")
        return pod
    except ApiException as e:
        if e.status == 409:
            print(f"Pod {pod_name} already exists. Reusing...")
            return v1.read_namespaced_pod(name=pod_name, namespace=NAMESPACE)
        raise


def _wait_for_node_assignment(v1: client.CoreV1Api, pod_name: str) -> str:
    deadline = time.time() + SCHEDULING_TIMEOUT_SECONDS
    while time.time() < deadline:
        pod = v1.read_namespaced_pod(name=pod_name, namespace=NAMESPACE)
        node_name = pod.spec.node_name
        if node_name:
            return node_name
        time.sleep(1)

    raise TimeoutError(f"Pod {pod_name} was not scheduled within {SCHEDULING_TIMEOUT_SECONDS} seconds")


def _wait_for_pod_ready(v1: client.CoreV1Api, pod_name: str) -> None:
    deadline = time.time() + SCHEDULING_TIMEOUT_SECONDS
    while time.time() < deadline:
        pod = v1.read_namespaced_pod(name=pod_name, namespace=NAMESPACE)
        if pod.status.phase == "Running":
            for condition in pod.status.conditions or []:
                if condition.type == "Ready" and condition.status == "True":
                    return
        time.sleep(1)

    raise TimeoutError(f"Pod {pod_name} was not ready within {SCHEDULING_TIMEOUT_SECONDS} seconds")


def _wait_for_service_endpoints(v1: client.CoreV1Api, service_name: str) -> None:
    deadline = time.time() + SCHEDULING_TIMEOUT_SECONDS
    while time.time() < deadline:
        endpoints = v1.read_namespaced_endpoints(name=service_name, namespace=NAMESPACE)
        for subset in endpoints.subsets or []:
            if subset.addresses:
                return
        time.sleep(1)

    raise TimeoutError(
        f"Service {service_name} had no ready endpoints within {SCHEDULING_TIMEOUT_SECONDS} seconds"
    )


def get_pro_service_name(session_id: str) -> str:
    return _session_resource_names(session_id)["pro_service_name"]


def get_pro_realtime_socketio_base_url(session_id: str) -> str:
    service_name = get_pro_service_name(session_id)
    return f"http://{service_name}.{NAMESPACE}.svc.cluster.local:{WORKER_PORT}"


async def run_session(session_id: str, project_id: str, user_id: str) -> SessionInfo:
    session_info = db.get_session(session_id)
    if not session_info:
        raise ValueError(f"Session {session_id} not found")

    config.load_kube_config(config_file=str(REMOTE_CONFIG_PATH))
    v1 = client.CoreV1Api()
    names = _session_resource_names(session_id)
    pro_pvc_name = names["pro_pvc_name"]
    pro_pod_name = names["pro_pod_name"]
    pro_service_name = names["pro_service_name"]

    _ensure_pvc(v1, pro_pvc_name)

    pro_pod_spec = _build_pod(
        pod_name=pro_pod_name,
        session_id=session_id,
        user_id=user_id,
        role="pro",
        image=_resolve_image(IMAGE_NAME_CODER_PRO, "IMAGE_NAME_CODER_PRO"),
        own_pvc_name=pro_pvc_name,
    )
    _create_or_reuse_pod(v1, pro_pod_spec)
    _ensure_service(
        v1,
        service_name=pro_service_name,
        selector={
            "task-id": session_id,
            "type": "session-worker",
            "role": "pro",
        },
    )
    _wait_for_pod_ready(v1, pro_pod_name)
    _wait_for_service_endpoints(v1, pro_service_name)

    return SessionInfo(id=session_id)

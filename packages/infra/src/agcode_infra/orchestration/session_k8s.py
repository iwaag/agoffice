import asyncio
import json
import http.client
import os
import re
import time

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.stream import portforward

from agcode_domain.schema import SessionInfo, TunnelInfo
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
HATCHET_CLIENT_TOKEN = os.getenv("HATCHET_CLIENT_TOKEN")
HATCHET_CLIENT_HOST_PORT = os.getenv("HATCHET_CLIENT_HOST_PORT")
HATCHET_CLIENT_SERVER_URL = os.getenv("HATCHET_CLIENT_SERVER_URL")
HATCHET_CLIENT_TLS_STRATEGY = os.getenv("HATCHET_CLIENT_TLS_STRATEGY", "none")
CLIENT_ID = os.getenv("CLIENT_ID", "agcode")

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


def _wait_for_pvc_bound(v1: client.CoreV1Api, pvc_name: str) -> None:
    deadline = time.time() + SCHEDULING_TIMEOUT_SECONDS
    while time.time() < deadline:
        pvc = v1.read_namespaced_persistent_volume_claim(name=pvc_name, namespace=NAMESPACE)
        if pvc.status.phase == "Bound":
            return
        time.sleep(1)

    pvc = v1.read_namespaced_persistent_volume_claim(name=pvc_name, namespace=NAMESPACE)
    raise TimeoutError(
        f"PVC {pvc_name} was not bound within {SCHEDULING_TIMEOUT_SECONDS} seconds "
        f"(phase={pvc.status.phase})"
    )


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
    token: str,
    role: str,
    image: str,
    own_pvc_name: str,
    peer_pvc_name: str | None = None,
    node_name: str | None = None
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
                        client.V1EnvVar(name="USER_ID", value=user_id),
                        client.V1EnvVar(name="AUTH_TOKEN", value=token),
                        client.V1EnvVar(name="AGENT_TIER", value="PRO"),
                        client.V1EnvVar(name="CLIENT_ID", value=CLIENT_ID),
                        client.V1EnvVar(name="HATCHET_CLIENT_TOKEN", value=HATCHET_CLIENT_TOKEN),
                        client.V1EnvVar(name="HATCHET_CLIENT_HOST_PORT", value=HATCHET_CLIENT_SERVER_URL),
                        client.V1EnvVar(name="HATCHET_CLIENT_SERVER_URL", value=HATCHET_CLIENT_SERVER_URL),
                        client.V1EnvVar(name="HATCHET_CLIENT_TLS_STRATEGY", value="none"),

                    ],
                )
            ],
            volumes=volumes,
        ),
    )


def _container_env_map(container: client.V1Container | None) -> dict[str, str | None]:
    if container is None or not container.env:
        return {}
    return {env.name: env.value for env in container.env}


def _pod_matches_spec(existing_pod: client.V1Pod, desired_pod: client.V1Pod) -> bool:
    existing_containers = existing_pod.spec.containers or []
    desired_containers = desired_pod.spec.containers or []
    if len(existing_containers) != len(desired_containers):
        return FalseCLIENT_ID

    for existing_container, desired_container in zip(existing_containers, desired_containers):
        if existing_container.name != desired_container.name:
            return False
        if existing_container.image != desired_container.image:
            return False
        if _container_env_map(existing_container) != _container_env_map(desired_container):
            return False

    return True


def _wait_for_pod_deleted(v1: client.CoreV1Api, pod_name: str) -> None:
    deadline = time.time() + SCHEDULING_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            v1.read_namespaced_pod(name=pod_name, namespace=NAMESPACE)
        except ApiException as e:
            if e.status == 404:
                return
            raise
        time.sleep(1)

    raise TimeoutError(f"Pod {pod_name} was not deleted within {SCHEDULING_TIMEOUT_SECONDS} seconds")


def _create_or_reuse_pod(v1: client.CoreV1Api, pod_spec: client.V1Pod) -> client.V1Pod:
    pod_name = pod_spec.metadata.name
    try:
        pod = v1.create_namespaced_pod(namespace=NAMESPACE, body=pod_spec)
        print(f"Pod {pod_name} created successfully.")
        return pod
    except ApiException as e:
        if e.status == 409:
            existing_pod = v1.read_namespaced_pod(name=pod_name, namespace=NAMESPACE)
            if _pod_matches_spec(existing_pod, pod_spec):
                print(f"Pod {pod_name} already exists. Reusing...")
                return existing_pod

            print(f"Pod {pod_name} already exists, but image/env changed. Recreating...")
            v1.delete_namespaced_pod(
                name=pod_name,
                namespace=NAMESPACE,
                grace_period_seconds=0,
            )
            _wait_for_pod_deleted(v1, pod_name)
            pod = v1.create_namespaced_pod(namespace=NAMESPACE, body=pod_spec)
            print(f"Pod {pod_name} recreated successfully.")
            return pod
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
        phase = pod.status.phase

        if phase in ("Failed", "Unknown"):
            conditions = pod.status.conditions or []
            reason = pod.status.reason or "unknown"
            message = pod.status.message or ""
            conditions_str = ", ".join(
                f"{c.type}={c.status}({c.reason})" for c in conditions if c.reason
            )
            raise RuntimeError(
                f"Pod {pod_name} entered terminal phase {phase}: "
                f"reason={reason} message={message} conditions=[{conditions_str}]"
            )

        if phase == "Running":
            for condition in pod.status.conditions or []:
                if condition.type == "Ready" and condition.status == "True":
                    return

        time.sleep(1)

    pod = v1.read_namespaced_pod(name=pod_name, namespace=NAMESPACE)
    phase = pod.status.phase
    conditions = pod.status.conditions or []
    conditions_str = ", ".join(
        f"{c.type}={c.status}(reason={c.reason}, msg={c.message})" for c in conditions
    )
    container_statuses = pod.status.container_statuses or []
    container_info = []
    for cs in container_statuses:
        state = cs.state
        if state.waiting:
            container_info.append(f"{cs.name}: waiting reason={state.waiting.reason} msg={state.waiting.message}")
        elif state.terminated:
            container_info.append(f"{cs.name}: terminated reason={state.terminated.reason} exit={state.terminated.exit_code}")
        else:
            container_info.append(f"{cs.name}: running")
    raise TimeoutError(
        f"Pod {pod_name} was not ready within {SCHEDULING_TIMEOUT_SECONDS} seconds "
        f"(phase={phase}, conditions=[{conditions_str}], containers=[{', '.join(container_info)}])"
    )


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


def _post_json_via_pod_portforward(
    v1: client.CoreV1Api,
    *,
    pod_name: str,
    path: str,
    payload: dict[str, str],
    timeout: float = 30.0,
) -> tuple[int, dict]:
    print(
        f"[start_tunnel] opening pod port-forward: namespace={NAMESPACE} pod={pod_name} "
        f"remote_port={WORKER_PORT} path={path} timeout={timeout}"
    )
    pf = portforward(
        v1.connect_get_namespaced_pod_portforward,
        pod_name,
        NAMESPACE,
        ports=str(WORKER_PORT),
    )
    print(f"[start_tunnel] port-forward object created for pod={pod_name}")

    initial_error = pf.error(WORKER_PORT)
    print(f"[start_tunnel] initial port-forward error for port {WORKER_PORT}: {initial_error}")

    sock = pf.socket(WORKER_PORT)
    sock.setblocking(True)
    sock.settimeout(timeout)
    print(f"[start_tunnel] acquired port-forward socket for pod={pod_name} port={WORKER_PORT}")

    connection = http.client.HTTPConnection("192.168.0.120", WORKER_PORT, timeout=timeout)
    connection.sock = sock
    try:
        print(f"[start_tunnel] sending HTTP request to worker via port-forward: path={path}")
        connection.request(
            "POST",
            path,
            body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        post_request_error = pf.error(WORKER_PORT)
        print(f"[start_tunnel] request sent. port-forward error for port {WORKER_PORT}: {post_request_error}")

        print(f"[start_tunnel] waiting for HTTP response from worker: pod={pod_name} path={path}")
        response = connection.getresponse()
        print(
            f"[start_tunnel] received HTTP response headers: status={response.status} "
            f"reason={response.reason}"
        )
        response_body = response.read()
        print(f"[start_tunnel] received HTTP response body bytes={len(response_body)}")
        portforward_error = pf.error(WORKER_PORT)
        if portforward_error is not None:
            raise RuntimeError(f"Pod port-forward failed: {portforward_error}")
        if not response_body:
            return response.status, {}
        return response.status, json.loads(response_body)
    except Exception as exc:
        current_error = None
        try:
            current_error = pf.error(WORKER_PORT)
        except Exception as pf_exc:
            current_error = f"<failed to read port-forward error: {pf_exc}>"
        print(
            f"[start_tunnel] port-forward request failed: type={type(exc).__name__} "
            f"error={exc} portforward_error={current_error}"
        )
        raise
    finally:
        print(f"[start_tunnel] closing HTTP connection for pod={pod_name}")
        connection.close()
        close_pf = getattr(pf, "close", None)
        if callable(close_pf):
            print(f"[start_tunnel] closing port-forward for pod={pod_name}")
            close_pf()


async def start_tunnel(session_id: str, tunnel_name: str, token: str) -> TunnelInfo:
    print(f"[start_tunnel] loading kubeconfig from {REMOTE_CONFIG_PATH}")
    config.load_kube_config(config_file=str(REMOTE_CONFIG_PATH))
    v1 = client.CoreV1Api()
    names = _session_resource_names(session_id)
    pod_name = names["pro_pod_name"]
    print(
        f"[start_tunnel] starting tunnel request: session_id={session_id} pod_name={pod_name} "
        f"tunnel_name={tunnel_name}"
    )
    print(f"[start_tunnel] waiting for pod readiness: pod={pod_name}")
    _wait_for_pod_ready(v1, pod_name)
    print(f"[start_tunnel] pod is ready: pod={pod_name}")
    status_code, payload = await asyncio.to_thread(
        _post_json_via_pod_portforward,
        v1,
        pod_name=pod_name,
        path="/tunnel/start",
        payload={
            "tunnel_name": tunnel_name,
            "host_token": token,
        },
    )
    print(f"[start_tunnel] worker response parsed: status_code={status_code} payload={payload}")
    if status_code >= 400:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        raise RuntimeError(detail or f"Tunnel start failed with status {status_code}")
    if payload.get("status") == "manual_auth_required":
        raise RuntimeError("Tunnel requires manual auth")
    return TunnelInfo(tunnel_name=payload["tunnel_name"])


async def run_session(session_id: str, project_id: str, user_id: str, token: str) -> SessionInfo:
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
        token=token,
        image=_resolve_image(IMAGE_NAME_CODER_PRO, "IMAGE_NAME_CODER_PRO"),
        own_pvc_name=pro_pvc_name,
    )
    _create_or_reuse_pod(v1, pro_pod_spec)
    _wait_for_node_assignment(v1, pro_pod_name)
    _wait_for_pvc_bound(v1, pro_pvc_name)
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

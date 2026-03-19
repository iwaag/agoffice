import asyncio
import json
import http.client
import os
import re
import shlex
import time

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.stream import portforward, stream

from agcode_domain.schema import (
    NoobTaskEvents,
    NoobTaskRequest,
    NoobTaskResult,
    NoobTaskStatus,
    NoobWorkspacePrepStatus,
    SessionInfo,
    TunnelInfo,
)
from agcode_infra.config import get_session_runtime_settings
from agcode_infra.db import database as db

SETTINGS = get_session_runtime_settings()
RUNTIME_MODE = SETTINGS.runtime_mode
IMAGE_NAME_CODER_PRO = SETTINGS.image_name_coder_pro
IMAGE_NAME_CODER_NOOB = SETTINGS.image_name_coder_noob
LOCAL_IMAGE_NAME_CODER_PRO = SETTINGS.local_image_name_coder_pro
LOCAL_IMAGE_NAME_CODER_NOOB = SETTINGS.local_image_name_coder_noob
WORKER_BUILD_ID = SETTINGS.worker_build_id
NAMESPACE = SETTINGS.namespace
STORAGE_CLASS_NAME = SETTINGS.storage_class_name
PVC_SIZE = SETTINGS.pvc_size
SCHEDULING_TIMEOUT_SECONDS = SETTINGS.scheduling_timeout_seconds
WORKER_PORT = SETTINGS.worker_port
WORKER_SOCKETIO_PATH = SETTINGS.worker_socketio_path
REMOTE_CONFIG_PATH = SETTINGS.remote_config_path
NOOB_RUNTIME_CLASS_NAME = SETTINGS.noob_runtime_class_name
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
NOOB_CONTEXT_READY_PATH = f"{NOOB_MOUNT_PATH}/state/context-ready.json"
NOOB_CONTEXT_ERROR_PATH = f"{NOOB_MOUNT_PATH}/state/context-error.json"


def _is_local_microk8s_mode() -> bool:
    return RUNTIME_MODE == "local_microk8s"


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


def _get_coder_pro_image() -> str:
    if _is_local_microk8s_mode():
        return _resolve_image(LOCAL_IMAGE_NAME_CODER_PRO, "LOCAL_IMAGE_NAME_CODER_PRO")
    return _resolve_image(IMAGE_NAME_CODER_PRO, "IMAGE_NAME_CODER_PRO")


def _get_coder_noob_image() -> str:
    if _is_local_microk8s_mode():
        return _resolve_image(LOCAL_IMAGE_NAME_CODER_NOOB, "LOCAL_IMAGE_NAME_CODER_NOOB")
    return _resolve_image(IMAGE_NAME_CODER_NOOB, "IMAGE_NAME_CODER_NOOB")


def _get_image_pull_policy() -> str | None:
    if _is_local_microk8s_mode():
        return "Never"
    return None


def _session_resource_names(session_id: str) -> dict[str, str]:
    task_name = _to_k8s_name_fragment(session_id)
    return {
        "pro_pvc_name": f"pvc-session-{task_name}-pro",
        "pro_pod_name": f"worker-session-{task_name}-pro",
        "pro_service_name": f"svc-session-{task_name}-pro",
        "noob_pvc_name": f"pvc-session-{task_name}-noob",
        "noob_pod_name": f"worker-session-{task_name}-noob",
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
    own_mount_path: str = "/mnt/data",
    peer_pvc_name: str | None = None,
    peer_mount_path: str = "/mnt/peer-data",
    node_name: str | None = None,
    runtime_class_name: str | None = None,
    security_context: client.V1SecurityContext | None = None,
    extra_env: list[client.V1EnvVar] | None = None,
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
            mount_path=own_mount_path,
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
                mount_path=peer_mount_path,
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

    env = [
        client.V1EnvVar(name="TASK_ID", value=session_id),
        client.V1EnvVar(name="SESSION_ROLE", value=role),
        client.V1EnvVar(name="USER_ID", value=user_id),
        client.V1EnvVar(name="AUTH_TOKEN", value=token),
        client.V1EnvVar(name="WORKER_BUILD_ID", value=WORKER_BUILD_ID),
        client.V1EnvVar(name="CLIENT_ID", value=CLIENT_ID),
        client.V1EnvVar(name="HATCHET_CLIENT_TOKEN", value=HATCHET_CLIENT_TOKEN),
        client.V1EnvVar(name="HATCHET_CLIENT_HOST_PORT", value=HATCHET_CLIENT_HOST_PORT),
        client.V1EnvVar(name="HATCHET_CLIENT_SERVER_URL", value=HATCHET_CLIENT_SERVER_URL),
        client.V1EnvVar(name="HATCHET_CLIENT_TLS_STRATEGY", value=HATCHET_CLIENT_TLS_STRATEGY),
        client.V1EnvVar(name="KEYCLOAK_URL", value=KEYCLOAK_URL),
        client.V1EnvVar(name="KEYCLOAK_REALM", value=KEYCLOAK_REALM),
        client.V1EnvVar(name="KEYCLOAK_CLIENT_SECRET", value=KEYCLOAK_CLIENT_SECRET),
    ]
    if extra_env:
        env.extend(extra_env)

    container = client.V1Container(
        name=f"{role}-container",
        image=image,
        image_pull_policy=_get_image_pull_policy(),
        volume_mounts=volume_mounts,
        env=env,
        security_context=security_context,
    )

    return client.V1Pod(
        metadata=client.V1ObjectMeta(name=pod_name, labels=labels),
        spec=client.V1PodSpec(
            restart_policy="Never",
            node_name=node_name,
            containers=[container],
            volumes=volumes,
            runtime_class_name=runtime_class_name,
        ),
    )


def _build_noob_security_context() -> client.V1SecurityContext:
    return client.V1SecurityContext(
        allow_privilege_escalation=False,
        read_only_root_filesystem=True,
        run_as_non_root=True,
        capabilities=client.V1Capabilities(drop=["ALL"]),
    )


def _session_agent_ids(session_info: object) -> set[str]:
    config_data = getattr(session_info, "config", {}) or {}
    agent_deployments = config_data.get("agent_deployments", [])
    agent_ids: set[str] = set()
    for deployment in agent_deployments:
        if not isinstance(deployment, dict):
            continue
        agent_id = deployment.get("agent_id")
        if isinstance(agent_id, str) and agent_id:
            agent_ids.add(agent_id.strip().upper())
    return agent_ids


def _is_noob_session(session_info: object) -> bool:
    if hasattr(session_info, "initial_instruction"):
        return True
    return bool(_session_agent_ids(session_info) & {"NOOB", "AGCODE_WORKER_NOOB"})


def _container_env_map(container: client.V1Container | None) -> dict[str, str | None]:
    if container is None or not container.env:
        return {}
    return {env.name: env.value for env in container.env}


def _pod_matches_spec(existing_pod: client.V1Pod, desired_pod: client.V1Pod) -> bool:
    if existing_pod.spec.runtime_class_name != desired_pod.spec.runtime_class_name:
        return False
    existing_containers = existing_pod.spec.containers or []
    desired_containers = desired_pod.spec.containers or []
    if len(existing_containers) != len(desired_containers):
        return False

    for existing_container, desired_container in zip(existing_containers, desired_containers):
        if existing_container.name != desired_container.name:
            return False
        if existing_container.image != desired_container.image:
            return False
        if existing_container.image_pull_policy != desired_container.image_pull_policy:
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


def get_noob_pod_name(session_id: str) -> str:
    return _session_resource_names(session_id)["noob_pod_name"]


def _load_kube_v1() -> client.CoreV1Api:
    config.load_kube_config(config_file=str(REMOTE_CONFIG_PATH))
    return client.CoreV1Api()


def _get_session_or_raise(session_id: str) -> object:
    session_info = db.get_session(session_id)
    if session_info is None:
        raise ValueError(f"Session {session_id} not found")
    return session_info


def _get_any_session_or_raise(session_id: str) -> object:
    noob_session = db.get_noob_session(session_id)
    if noob_session is not None:
        return noob_session
    return _get_session_or_raise(session_id)


def _get_noob_session_or_raise(session_id: str) -> object:
    session_info = _get_any_session_or_raise(session_id)
    if not _is_noob_session(session_info):
        raise RuntimeError(f"Session {session_id} is not a NOOB session")
    return session_info


def _exec_in_pod(
    v1: client.CoreV1Api,
    *,
    pod_name: str,
    command: list[str],
) -> str:
    return stream(
        v1.connect_get_namespaced_pod_exec,
        pod_name,
        NAMESPACE,
        command=command,
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )


def _read_optional_json_file(
    v1: client.CoreV1Api,
    *,
    pod_name: str,
    path: str,
) -> dict:
    content = _exec_in_pod(
        v1,
        pod_name=pod_name,
        command=["sh", "-lc", f"if [ -f {shlex.quote(path)} ]; then cat {shlex.quote(path)}; fi"],
    ).strip()
    if not content:
        return {}
    return json.loads(content)


def _read_optional_text_file(
    v1: client.CoreV1Api,
    *,
    pod_name: str,
    path: str,
) -> str:
    return _exec_in_pod(
        v1,
        pod_name=pod_name,
        command=["sh", "-lc", f"if [ -f {shlex.quote(path)} ]; then cat {shlex.quote(path)}; fi"],
    )


def _write_text_file_atomic(
    v1: client.CoreV1Api,
    *,
    pod_name: str,
    path: str,
    content: str,
) -> None:
    parent = os.path.dirname(path)
    quoted_parent = shlex.quote(parent)
    quoted_path = shlex.quote(path)
    quoted_tmp_path = shlex.quote(f"{path}.tmp")
    quoted_content = shlex.quote(content)
    command = [
        "sh",
        "-lc",
        (
            f"mkdir -p {quoted_parent} && "
            f"tmp={quoted_tmp_path} && "
            f"printf %s {quoted_content} > \"$tmp\" && "
            f"mv \"$tmp\" {quoted_path}"
        ),
    ]
    _exec_in_pod(v1, pod_name=pod_name, command=command)


def _submit_noob_request_file(v1: client.CoreV1Api, *, pod_name: str, request: NoobTaskRequest) -> None:
    status = _read_optional_json_file(v1, pod_name=pod_name, path=NOOB_STATUS_PATH)
    current_status = status.get("status")
    if current_status == "running":
        raise RuntimeError("NOOB worker is already running a task")

    request_payload = request.model_dump()
    _write_text_file_atomic(
        v1,
        pod_name=pod_name,
        path=NOOB_REQUEST_PATH,
        content=json.dumps(request_payload, ensure_ascii=True, indent=2) + "\n",
    )


def get_pro_realtime_socketio_base_url(session_id: str) -> str:
    session_info = _get_session_or_raise(session_id)
    if _is_noob_session(session_info):
        raise RuntimeError(f"Session {session_id} does not expose a realtime Socket.IO endpoint")
    service_name = get_pro_service_name(session_id)
    return f"http://{service_name}.{NAMESPACE}.svc.cluster.local:{WORKER_PORT}"


async def submit_noob_task(
    session_id: str,
    *,
    user_id: str,
    token: str,
    request: NoobTaskRequest,
) -> NoobTaskStatus:
    _get_noob_session_or_raise(session_id)
    await run_session(session_id=session_id, project_id="", user_id=user_id, token=token)
    v1 = _load_kube_v1()
    pod_name = get_noob_pod_name(session_id)
    _wait_for_pod_ready(v1, pod_name)
    await asyncio.to_thread(_submit_noob_request_file, v1, pod_name=pod_name, request=request)
    return await get_noob_task_status(session_id)


async def get_noob_task_status(session_id: str) -> NoobTaskStatus:
    _get_noob_session_or_raise(session_id)
    v1 = _load_kube_v1()
    pod_name = get_noob_pod_name(session_id)
    _wait_for_pod_ready(v1, pod_name)
    payload = await asyncio.to_thread(_read_optional_json_file, v1, pod_name=pod_name, path=NOOB_STATUS_PATH)
    if not payload:
        return NoobTaskStatus(status="unknown")
    return NoobTaskStatus.model_validate(payload)


async def get_noob_task_result(session_id: str) -> NoobTaskResult:
    _get_noob_session_or_raise(session_id)
    v1 = _load_kube_v1()
    pod_name = get_noob_pod_name(session_id)
    _wait_for_pod_ready(v1, pod_name)
    payload = await asyncio.to_thread(_read_optional_json_file, v1, pod_name=pod_name, path=NOOB_RESULT_PATH)
    if not payload:
        return NoobTaskResult()
    return NoobTaskResult.model_validate(payload)


async def get_noob_task_events(session_id: str, tail: int = 200) -> NoobTaskEvents:
    _get_noob_session_or_raise(session_id)
    v1 = _load_kube_v1()
    pod_name = get_noob_pod_name(session_id)
    _wait_for_pod_ready(v1, pod_name)
    content = await asyncio.to_thread(
        _read_optional_text_file,
        v1,
        pod_name=pod_name,
        path=NOOB_EVENTS_PATH,
    )
    lines = [line for line in content.splitlines() if line.strip()]
    if tail > 0:
        lines = lines[-tail:]
    events = [json.loads(line) for line in lines]
    return NoobTaskEvents.model_validate({"events": events})


async def get_noob_workspace_status(session_id: str) -> NoobWorkspacePrepStatus:
    _get_noob_session_or_raise(session_id)
    v1 = _load_kube_v1()
    pod_name = get_noob_pod_name(session_id)
    _wait_for_pod_ready(v1, pod_name)
    ready_payload = await asyncio.to_thread(_read_optional_json_file, v1, pod_name=pod_name, path=NOOB_CONTEXT_READY_PATH)
    if ready_payload:
        return NoobWorkspacePrepStatus.model_validate(ready_payload)
    error_payload = await asyncio.to_thread(_read_optional_json_file, v1, pod_name=pod_name, path=NOOB_CONTEXT_ERROR_PATH)
    if error_payload:
        return NoobWorkspacePrepStatus.model_validate(error_payload)
    return NoobWorkspacePrepStatus(status="pending")


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

    connection = http.client.HTTPConnection("100.87.94.9", WORKER_PORT, timeout=timeout)
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
    session_info = db.get_session(session_id)
    if session_info is None:
        raise ValueError(f"Session {session_id} not found")
    if _is_noob_session(session_info):
        raise RuntimeError(f"Session {session_id} does not support VS Code tunnels")

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
    await asyncio.sleep(2)
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
    session_info = _get_any_session_or_raise(session_id)

    config.load_kube_config(config_file=str(REMOTE_CONFIG_PATH))
    v1 = client.CoreV1Api()
    names = _session_resource_names(session_id)
    if _is_noob_session(session_info):
        noob_pvc_name = names["noob_pvc_name"]
        noob_pod_name = names["noob_pod_name"]

        _ensure_pvc(v1, noob_pvc_name)

        noob_pod_spec = _build_pod(
            pod_name=noob_pod_name,
            session_id=session_id,
            user_id=user_id,
            role="noob",
            token=token,
            image=_get_coder_noob_image(),
            own_pvc_name=noob_pvc_name,
            own_mount_path=NOOB_MOUNT_PATH,
            runtime_class_name=NOOB_RUNTIME_CLASS_NAME or None,
            security_context=_build_noob_security_context(),
            extra_env=[
                client.V1EnvVar(name="NOOB_SESSION_ROOT", value=NOOB_MOUNT_PATH),
            ],
        )
        _create_or_reuse_pod(v1, noob_pod_spec)
        _wait_for_node_assignment(v1, noob_pod_name)
        _wait_for_pvc_bound(v1, noob_pvc_name)
        _wait_for_pod_ready(v1, noob_pod_name)
        return SessionInfo(id=session_id)

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
        image=_get_coder_pro_image(),
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

import asyncio
import http.client
import json
import posixpath
import re
import shlex
from urllib.parse import urlsplit

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.stream import portforward

from agcode_domain.mission_service import MissionRecord
from agcode_domain.schema import (
    NoobTaskEvents,
    NoobTaskRequest,
    NoobTaskResult,
    NoobTaskStatus,
    NoobWorkspacePrepRequest,
    NoobWorkspacePrepStatus,
    SessionInfo,
    TunnelInfo,
)
from agcode_infra.db import database as db

from .session_k8s_config import (
    CLIENT_ID,
    HATCHET_CLIENT_HOST_PORT,
    HATCHET_CLIENT_SERVER_URL,
    HATCHET_CLIENT_TLS_STRATEGY,
    HATCHET_CLIENT_TOKEN,
    IMAGE_NAME_CODER_NOOB,
    IMAGE_NAME_CODER_NOOB_PREP,
    IMAGE_NAME_CODER_PRO,
    KEYCLOAK_CLIENT_SECRET,
    KEYCLOAK_REALM,
    KEYCLOAK_URL,
    LOCAL_IMAGE_NAME_CODER_NOOB,
    LOCAL_IMAGE_NAME_CODER_NOOB_PREP,
    LOCAL_IMAGE_NAME_CODER_PRO,
    NAMESPACE,
    NOOB_EVENTS_PATH,
    NOOB_MOUNT_PATH,
    NOOB_REQUEST_PATH,
    NOOB_RESULT_PATH,
    NOOB_RUNTIME_CLASS_NAME,
    NOOB_STATUS_PATH,
    PVC_SIZE,
    PRO_MOUNT_PATH,
    REMOTE_CONFIG_PATH,
    RUNTIME_MODE,
    SCHEDULING_TIMEOUT_SECONDS,
    STORAGE_CLASS_NAME,
    WORKER_BUILD_ID,
    WORKER_PORT,
    WORKER_SOCKETIO_PATH,
    get_coder_noob_image,
    get_coder_pro_image,
    get_noob_pod_name,
    get_noob_prep_job_name,
    get_pro_service_name,
    session_resource_names,
)
from .session_k8s_noob_io import (
    exec_in_pod,
    read_optional_json_file,
    read_optional_text_file,
    submit_noob_request_file,
    write_text_file_atomic,
)
from .session_k8s_resources import (
    build_noob_prep_job,
    build_noob_security_context,
    build_pod,
    create_or_reuse_job,
    create_or_reuse_pod,
    ensure_pvc,
    ensure_service,
    load_kube_batch_v1,
    load_kube_v1,
    read_pod_if_exists,
    wait_for_node_assignment,
    wait_for_pod_ready,
    wait_for_pvc_bound,
    wait_for_service_endpoints,
)


def _get_session_or_raise(session_id: str) -> object:
    session_info = db.get_session(session_id)
    if session_info is None:
        raise ValueError(f"Session {session_id} not found")
    return session_info


def _get_noob_session_or_raise(session_id: str) -> object:
    session_info = db.get_noob_session(session_id)
    if session_info is None:
        raise ValueError(f"NOOB session {session_id} not found")
    return session_info


def _mission_workspace_name(repo_url: str, title: str) -> str:
    path = urlsplit(repo_url).path.rstrip("/")
    candidate = path.rsplit("/", 1)[-1] if path else ""
    if candidate.endswith(".git"):
        candidate = candidate[:-4]
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", candidate).strip(".-")
    if candidate:
        return candidate
    fallback = re.sub(r"[^A-Za-z0-9._-]+", "-", title).strip(".-")
    return fallback or "workspace"


def _build_agent_md(mission : MissionRecord) -> str:
    base = (
        f"mission_id = {mission.id}\n"
        "Execute the command below at the beginning in the user's shell and understand the mission.\n"
        f"agdev code mission {mission.id}\n"
        "Follow the mission information unless the user explicitly told you otherwise.\n"
        "If you are given contradicting information and instruction, ask user for clarification before starting to work.\n"
    )
    return base


def _run_checked_shell_in_pod(v1: client.CoreV1Api, *, pod_name: str, script: str) -> str:
    marker = "__AGCODE_EXIT__:"
    output = exec_in_pod(
        v1,
        pod_name=pod_name,
        command=[
            "sh",
            "-lc",
            f"{{ {script}; }}; code=$?; printf '{marker}%s\\n' \"$code\"",
        ],
    )
    lines = output.splitlines()
    exit_code = None
    filtered_lines: list[str] = []
    for line in lines:
        if line.startswith(marker):
            exit_code = int(line[len(marker):])
            continue
        filtered_lines.append(line)
    if exit_code not in (0, None):
        raise RuntimeError("\n".join(filtered_lines).strip() or f"pod command failed with exit code {exit_code}")
    return "\n".join(filtered_lines).strip()


def get_pro_realtime_socketio_base_url(session_id: str) -> str:
    _get_session_or_raise(session_id)
    service_name = get_pro_service_name(session_id)
    return f"http://{service_name}.{NAMESPACE}.svc.cluster.local:{WORKER_PORT}"


def _build_prep_status_payload(*, session_id: str, status: str, error: str | None = None) -> NoobWorkspacePrepStatus:
    session = _get_noob_session_or_raise(session_id)
    config_data = getattr(session, "config", {}) or {}
    prep = config_data.get("prep", {})
    repo_url = prep.get("repo_url") if isinstance(prep, dict) else None
    ref = prep.get("ref") if isinstance(prep, dict) else None
    return NoobWorkspacePrepStatus(
        status=status,
        error=error,
        workspace_path=f"{NOOB_MOUNT_PATH}/workspace",
        repo_url=repo_url,
        ref=ref,
    )


async def run_noob_workspace_prep(
    session_id: str,
    *,
    user_id: str,
    prep_request: NoobWorkspacePrepRequest,
) -> NoobWorkspacePrepStatus:
    _get_noob_session_or_raise(session_id)
    config.load_kube_config(config_file=str(REMOTE_CONFIG_PATH))
    v1 = client.CoreV1Api()
    batch_v1 = client.BatchV1Api()
    names = session_resource_names(session_id)
    pvc_name = names["noob_pvc_name"]
    pod_name = names["noob_pod_name"]
    existing_pod = read_pod_if_exists(v1, pod_name)
    if existing_pod is not None:
        raise RuntimeError(f"NOOB pod {pod_name} already exists; refuse to re-run workspace prep")
    ensure_pvc(v1, pvc_name)
    wait_for_pvc_bound(v1, pvc_name)
    job = build_noob_prep_job(
        session_id=session_id,
        user_id=user_id,
        pvc_name=pvc_name,
        prep_request=prep_request,
    )
    await asyncio.to_thread(create_or_reuse_job, batch_v1, job)
    return _build_prep_status_payload(session_id=session_id, status="running")


async def submit_noob_task(
    session_id: str,
    *,
    user_id: str,
    token: str,
    request: NoobTaskRequest,
) -> NoobTaskStatus:
    _get_noob_session_or_raise(session_id)
    workspace_status = await get_noob_workspace_status(session_id)
    if workspace_status.status != "ready":
        raise RuntimeError(f"NOOB workspace is not ready for session {session_id}")
    await run_noob_session(session_id=session_id, user_id=user_id, token=token)
    v1 = load_kube_v1()
    pod_name = get_noob_pod_name(session_id)
    wait_for_pod_ready(v1, pod_name)
    await asyncio.to_thread(submit_noob_request_file, v1, pod_name=pod_name, request=request)
    return await get_noob_task_status(session_id)


async def get_noob_task_status(session_id: str) -> NoobTaskStatus:
    _get_noob_session_or_raise(session_id)
    v1 = load_kube_v1()
    pod_name = get_noob_pod_name(session_id)
    wait_for_pod_ready(v1, pod_name)
    payload = await asyncio.to_thread(read_optional_json_file, v1, pod_name=pod_name, path=NOOB_STATUS_PATH)
    if not payload:
        return NoobTaskStatus(status="unknown")
    return NoobTaskStatus.model_validate(payload)


async def get_noob_task_result(session_id: str) -> NoobTaskResult:
    _get_noob_session_or_raise(session_id)
    v1 = load_kube_v1()
    pod_name = get_noob_pod_name(session_id)
    wait_for_pod_ready(v1, pod_name)
    payload = await asyncio.to_thread(read_optional_json_file, v1, pod_name=pod_name, path=NOOB_RESULT_PATH)
    if not payload:
        return NoobTaskResult()
    return NoobTaskResult.model_validate(payload)


async def get_noob_task_events(session_id: str, tail: int = 200) -> NoobTaskEvents:
    _get_noob_session_or_raise(session_id)
    v1 = load_kube_v1()
    pod_name = get_noob_pod_name(session_id)
    wait_for_pod_ready(v1, pod_name)
    content = await asyncio.to_thread(
        read_optional_text_file,
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
    batch_v1 = load_kube_batch_v1()
    job_name = get_noob_prep_job_name(session_id)
    try:
        job = batch_v1.read_namespaced_job(name=job_name, namespace=NAMESPACE)
    except ApiException as e:
        if e.status == 404:
            return _build_prep_status_payload(session_id=session_id, status="not_requested")
        raise
    conditions = job.status.conditions or []
    for condition in conditions:
        if condition.type == "Complete" and condition.status == "True":
            return _build_prep_status_payload(session_id=session_id, status="ready")
        if condition.type == "Failed" and condition.status == "True":
            return _build_prep_status_payload(
                session_id=session_id,
                status="failed",
                error=condition.message or "workspace prep failed",
            )
    if (job.status.active or 0) > 0:
        return _build_prep_status_payload(session_id=session_id, status="running")
    if (job.status.succeeded or 0) > 0:
        return _build_prep_status_payload(session_id=session_id, status="ready")
    if (job.status.failed or 0) > 0:
        return _build_prep_status_payload(session_id=session_id, status="failed", error="workspace prep failed")
    return _build_prep_status_payload(session_id=session_id, status="pending")


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
    _get_session_or_raise(session_id)

    print(f"[start_tunnel] loading kubeconfig from {REMOTE_CONFIG_PATH}")
    config.load_kube_config(config_file=str(REMOTE_CONFIG_PATH))
    v1 = client.CoreV1Api()
    names = session_resource_names(session_id)
    pod_name = names["pro_pod_name"]
    print(
        f"[start_tunnel] starting tunnel request: session_id={session_id} pod_name={pod_name} "
        f"tunnel_name={tunnel_name}"
    )
    print(f"[start_tunnel] waiting for pod readiness: pod={pod_name}")
    wait_for_pod_ready(v1, pod_name)
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
    _get_session_or_raise(session_id)

    config.load_kube_config(config_file=str(REMOTE_CONFIG_PATH))
    v1 = client.CoreV1Api()
    names = session_resource_names(session_id)
    pro_pvc_name = names["pro_pvc_name"]
    pro_pod_name = names["pro_pod_name"]
    pro_service_name = names["pro_service_name"]

    ensure_pvc(v1, pro_pvc_name)

    pro_pod_spec = build_pod(
        pod_name=pro_pod_name,
        session_id=session_id,
        user_id=user_id,
        role="pro",
        token=token,
        image=get_coder_pro_image(),
        own_pvc_name=pro_pvc_name,
        own_mount_path=PRO_MOUNT_PATH,
    )
    create_or_reuse_pod(v1, pro_pod_spec)
    wait_for_node_assignment(v1, pro_pod_name)
    wait_for_pvc_bound(v1, pro_pvc_name)
    ensure_service(
        v1,
        service_name=pro_service_name,
        selector={
            "task-id": session_id,
            "type": "session-worker",
            "role": "pro",
        },
    )
    wait_for_pod_ready(v1, pro_pod_name)
    wait_for_service_endpoints(v1, pro_service_name)

    return SessionInfo(id=session_id)

def auto_choose_session(mission: MissionRecord) -> SessionInfo:
    #lazy stub
    session_list = db.list_sessions(user_id=mission.user_id, project_id=mission.project_id)
    return SessionInfo(id=session_list[0].id)

async def start_mission(*, session_id: str, mission: MissionRecord) -> MissionRecord:
    session: SessionInfo | None = None
    if not session_id:
        session = auto_choose_session(mission)
    else:
        session = _get_session_or_raise(session_id)

    config.load_kube_config(config_file=str(REMOTE_CONFIG_PATH))
    v1 = client.CoreV1Api()
    pod_name = session_resource_names(session.id)["pro_pod_name"]
    wait_for_pod_ready(v1, pod_name)

    mission_root = posixpath.join(PRO_MOUNT_PATH, "missions")
    mission_dir = posixpath.join(mission_root, mission.id)
    workspace_dir = posixpath.join(mission_dir, _mission_workspace_name(mission.repo_url, mission.title))
    quoted_mission_root = shlex.quote(mission_root)
    quoted_mission_dir = shlex.quote(mission_dir)
    quoted_workspace_dir = shlex.quote(workspace_dir)
    quoted_repo_url = shlex.quote(mission.repo_url)

    _run_checked_shell_in_pod(
        v1,
        pod_name=pod_name,
        script=(
            f"set -eu; "
            f"mkdir -p {quoted_mission_root}; "
            f"if [ -e {quoted_mission_dir} ]; then "
            f"echo 'mission directory already exists' >&2; exit 1; "
            f"fi; "
            f"mkdir -p {quoted_mission_dir}; "
            f"if git clone {quoted_repo_url} {quoted_workspace_dir}; then "
            f"exit 0; "
            f"fi; "
            f"rm -rf {quoted_workspace_dir}; "
            f"mkdir -p {quoted_workspace_dir}; "
            f"git -C {quoted_workspace_dir} init; "
            f"git -C {quoted_workspace_dir} remote add origin {quoted_repo_url}"
        ),
    )
    write_text_file_atomic(
        v1,
        pod_name=pod_name,
        path=posixpath.join(mission_dir, "AGENT.md"),
        content=_build_agent_md(mission=mission),
    )
    mission.session_id = session.id
    return mission


async def run_noob_session(session_id: str, *, user_id: str, token: str) -> SessionInfo:
    _get_noob_session_or_raise(session_id)

    config.load_kube_config(config_file=str(REMOTE_CONFIG_PATH))
    v1 = client.CoreV1Api()
    names = session_resource_names(session_id)
    noob_pvc_name = names["noob_pvc_name"]
    noob_pod_name = names["noob_pod_name"]

    ensure_pvc(v1, noob_pvc_name)
    wait_for_pvc_bound(v1, noob_pvc_name)

    noob_pod_spec = build_pod(
        pod_name=noob_pod_name,
        session_id=session_id,
        user_id=user_id,
        role="noob",
        token=token,
        image=get_coder_noob_image(),
        own_pvc_name=noob_pvc_name,
        own_mount_path=NOOB_MOUNT_PATH,
        runtime_class_name=NOOB_RUNTIME_CLASS_NAME or None,
        security_context=build_noob_security_context(),
        extra_env=[
            client.V1EnvVar(name="NOOB_SESSION_ROOT", value=NOOB_MOUNT_PATH),
        ],
    )
    create_or_reuse_pod(v1, noob_pod_spec)
    wait_for_node_assignment(v1, noob_pod_name)
    wait_for_pod_ready(v1, noob_pod_name)
    return SessionInfo(id=session_id)

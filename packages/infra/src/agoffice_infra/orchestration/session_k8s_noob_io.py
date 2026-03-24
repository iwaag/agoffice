import json
import os
import shlex

from kubernetes import client
from kubernetes.stream import stream

from agoffice_domain.schema import NoobTaskRequest

from .session_k8s_config import NOOB_REQUEST_PATH, NOOB_STATUS_PATH, NAMESPACE


def exec_in_pod(
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


def read_optional_json_file(
    v1: client.CoreV1Api,
    *,
    pod_name: str,
    path: str,
) -> dict:
    content = exec_in_pod(
        v1,
        pod_name=pod_name,
        command=["sh", "-lc", f"if [ -f {shlex.quote(path)} ]; then cat {shlex.quote(path)}; fi"],
    ).strip()
    if not content:
        return {}
    return json.loads(content)


def read_optional_text_file(
    v1: client.CoreV1Api,
    *,
    pod_name: str,
    path: str,
) -> str:
    return exec_in_pod(
        v1,
        pod_name=pod_name,
        command=["sh", "-lc", f"if [ -f {shlex.quote(path)} ]; then cat {shlex.quote(path)}; fi"],
    )


def write_text_file_atomic(
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
    exec_in_pod(v1, pod_name=pod_name, command=command)


def submit_noob_request_file(v1: client.CoreV1Api, *, pod_name: str, request: NoobTaskRequest) -> None:
    status = read_optional_json_file(v1, pod_name=pod_name, path=NOOB_STATUS_PATH)
    current_status = status.get("status")
    if current_status == "running":
        raise RuntimeError("NOOB worker is already running a task")

    request_payload = request.model_dump()
    write_text_file_atomic(
        v1,
        pod_name=pod_name,
        path=NOOB_REQUEST_PATH,
        content=json.dumps(request_payload, ensure_ascii=True, indent=2) + "\n",
    )

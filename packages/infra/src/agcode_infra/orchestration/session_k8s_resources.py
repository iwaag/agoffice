import time

from kubernetes import client, config
from kubernetes.client.rest import ApiException

from agcode_domain.schema import NoobWorkspacePrepRequest

from .session_k8s_config import (
    CLIENT_ID,
    HATCHET_CLIENT_HOST_PORT,
    HATCHET_CLIENT_SERVER_URL,
    HATCHET_CLIENT_TLS_STRATEGY,
    HATCHET_CLIENT_TOKEN,
    KEYCLOAK_CLIENT_SECRET,
    KEYCLOAK_REALM,
    KEYCLOAK_URL,
    NAMESPACE,
    NOOB_MOUNT_PATH,
    PVC_SIZE,
    REMOTE_CONFIG_PATH,
    SCHEDULING_TIMEOUT_SECONDS,
    STORAGE_CLASS_NAME,
    WORKER_BUILD_ID,
    WORKER_PORT,
    AGCODE_API_URL,
    AGCORE_API_URL,
    AGVIDEO_API_URL,
    get_coder_noob_prep_image,
    get_image_pull_policy,
    get_noob_prep_job_name,
)


def load_kube_v1() -> client.CoreV1Api:
    config.load_kube_config(config_file=str(REMOTE_CONFIG_PATH))
    return client.CoreV1Api()


def load_kube_batch_v1() -> client.BatchV1Api:
    config.load_kube_config(config_file=str(REMOTE_CONFIG_PATH))
    return client.BatchV1Api()


def ensure_pvc(v1: client.CoreV1Api, pvc_name: str) -> None:
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


def wait_for_pvc_bound(v1: client.CoreV1Api, pvc_name: str) -> None:
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


def ensure_service(
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


def build_pod(
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
        client.V1EnvVar(name="KEYCLOAK_CLIENT_ID", value="agdev"),
        client.V1EnvVar(name="AGVIDEO_API_URL", value=AGVIDEO_API_URL),
        client.V1EnvVar(name="AGCORE_API_URL", value=AGCORE_API_URL),
        client.V1EnvVar(name="AGCODE_API_URL", value=AGCODE_API_URL),
        
    ]
    if extra_env:
        env.extend(extra_env)

    container = client.V1Container(
        name=f"{role}-container",
        image=image,
        image_pull_policy=get_image_pull_policy(),
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


def build_noob_security_context() -> client.V1SecurityContext:
    return client.V1SecurityContext(
        allow_privilege_escalation=False,
        read_only_root_filesystem=True,
        run_as_non_root=True,
        capabilities=client.V1Capabilities(drop=["ALL"]),
    )


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
        # Temporarily ignore env drift for pod reuse.
        # Request-scoped values like AUTH_TOKEN change across API calls and were
        # forcing pro worker pods to be recreated even when the running pod
        # should be reused.
        # if _container_env_map(existing_container) != _container_env_map(desired_container):
        #     return False

    return True


def wait_for_pod_deleted(v1: client.CoreV1Api, pod_name: str) -> None:
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


def read_pod_if_exists(v1: client.CoreV1Api, pod_name: str) -> client.V1Pod | None:
    try:
        return v1.read_namespaced_pod(name=pod_name, namespace=NAMESPACE)
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def create_or_reuse_pod(v1: client.CoreV1Api, pod_spec: client.V1Pod) -> client.V1Pod:
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
            wait_for_pod_deleted(v1, pod_name)
            pod = v1.create_namespaced_pod(namespace=NAMESPACE, body=pod_spec)
            print(f"Pod {pod_name} recreated successfully.")
            return pod
        raise


def wait_for_node_assignment(v1: client.CoreV1Api, pod_name: str) -> str:
    deadline = time.time() + SCHEDULING_TIMEOUT_SECONDS
    while time.time() < deadline:
        pod = v1.read_namespaced_pod(name=pod_name, namespace=NAMESPACE)
        node_name = pod.spec.node_name
        if node_name:
            return node_name
        time.sleep(1)

    raise TimeoutError(f"Pod {pod_name} was not scheduled within {SCHEDULING_TIMEOUT_SECONDS} seconds")


def wait_for_pod_ready(v1: client.CoreV1Api, pod_name: str) -> None:
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


def _job_env_map(container: client.V1Container | None) -> dict[str, str | None]:
    if container is None or not container.env:
        return {}
    return {env.name: env.value for env in container.env}


def _job_matches_spec(existing_job: client.V1Job, desired_job: client.V1Job) -> bool:
    existing_template = existing_job.spec.template
    desired_template = desired_job.spec.template
    existing_containers = existing_template.spec.containers or []
    desired_containers = desired_template.spec.containers or []
    if len(existing_containers) != len(desired_containers):
        return False

    for existing_container, desired_container in zip(existing_containers, desired_containers):
        if existing_container.name != desired_container.name:
            return False
        if existing_container.image != desired_container.image:
            return False
        if existing_container.image_pull_policy != desired_container.image_pull_policy:
            return False
        if _job_env_map(existing_container) != _job_env_map(desired_container):
            return False

    return True


def wait_for_job_deleted(batch_v1: client.BatchV1Api, job_name: str) -> None:
    deadline = time.time() + SCHEDULING_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            batch_v1.read_namespaced_job(name=job_name, namespace=NAMESPACE)
        except ApiException as e:
            if e.status == 404:
                return
            raise
        time.sleep(1)
    raise TimeoutError(f"Job {job_name} was not deleted within {SCHEDULING_TIMEOUT_SECONDS} seconds")


def create_or_reuse_job(batch_v1: client.BatchV1Api, job_spec: client.V1Job) -> client.V1Job:
    job_name = job_spec.metadata.name
    try:
        job = batch_v1.create_namespaced_job(namespace=NAMESPACE, body=job_spec)
        print(f"Job {job_name} created successfully.")
        return job
    except ApiException as e:
        if e.status != 409:
            raise
        existing_job = batch_v1.read_namespaced_job(name=job_name, namespace=NAMESPACE)
        if _job_matches_spec(existing_job, job_spec):
            active = (existing_job.status.active or 0) > 0
            succeeded = (existing_job.status.succeeded or 0) > 0
            if active:
                print(f"Job {job_name} already exists and is still running. Reusing...")
                return existing_job
            if succeeded:
                print(f"Job {job_name} already exists and succeeded. Recreating for a fresh prep run...")
            else:
                print(f"Job {job_name} already exists and will be recreated.")
        batch_v1.delete_namespaced_job(
            name=job_name,
            namespace=NAMESPACE,
            propagation_policy="Background",
        )
        wait_for_job_deleted(batch_v1, job_name)
        job = batch_v1.create_namespaced_job(namespace=NAMESPACE, body=job_spec)
        print(f"Job {job_name} recreated successfully.")
        return job


def wait_for_service_endpoints(v1: client.CoreV1Api, service_name: str) -> None:
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


def build_noob_prep_job(
    *,
    session_id: str,
    user_id: str,
    pvc_name: str,
    prep_request: NoobWorkspacePrepRequest,
) -> client.V1Job:
    job_name = get_noob_prep_job_name(session_id)
    labels = {
        "task-id": session_id,
        "user-id": user_id,
        "type": "session-worker",
        "role": "noob-prep",
    }
    spec = prep_request.spec
    env = [
        client.V1EnvVar(name="NOOB_SESSION_ROOT", value=NOOB_MOUNT_PATH),
        client.V1EnvVar(name="PREP_REPO_URL", value=spec.repo_url),
        client.V1EnvVar(name="PREP_REF", value=spec.ref or ""),
        client.V1EnvVar(name="PREP_DEPTH", value=str(spec.depth or "")),
        client.V1EnvVar(name="PREP_SUB_PATH", value=spec.sub_path or ""),
        client.V1EnvVar(name="PREP_MODE", value=spec.mode),
    ]

    container = client.V1Container(
        name="noob-prep-container",
        image=get_coder_noob_prep_image(),
        image_pull_policy=get_image_pull_policy(),
        env=env,
        volume_mounts=[
            client.V1VolumeMount(
                name="session-data",
                mount_path=NOOB_MOUNT_PATH,
                read_only=False,
            )
        ],
    )
    pod_template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels=labels),
        spec=client.V1PodSpec(
            restart_policy="Never",
            containers=[container],
            volumes=[
                client.V1Volume(
                    name="session-data",
                    persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                        claim_name=pvc_name,
                        read_only=False,
                    ),
                )
            ],
        ),
    )
    return client.V1Job(
        metadata=client.V1ObjectMeta(name=job_name, labels=labels),
        spec=client.V1JobSpec(
            template=pod_template,
            backoff_limit=0,
        ),
    )

#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TARGET="${1:-pro}"
IMAGE_ENV_NAME="LOCAL_IMAGE_NAME_CODER_PRO"
DEFAULT_IMAGE_NAME="agoffice/coder-pro:dev"
DOCKERFILE_PATH="${ROOT_DIR}/deploy/docker/worker-pro.Dockerfile"

case "${TARGET}" in
  pro)
    IMAGE_ENV_NAME="LOCAL_IMAGE_NAME_CODER_PRO"
    DEFAULT_IMAGE_NAME="agoffice/coder-pro:dev"
    DOCKERFILE_PATH="${ROOT_DIR}/deploy/docker/worker-pro.Dockerfile"
    ;;
  noob)
    IMAGE_ENV_NAME="LOCAL_IMAGE_NAME_CODER_NOOB"
    DEFAULT_IMAGE_NAME="agoffice/coder-noob:dev"
    DOCKERFILE_PATH="${ROOT_DIR}/deploy/docker/worker-noob.Dockerfile"
    ;;
  *)
    echo "usage: $0 [pro|noob]" >&2
    exit 1
    ;;
esac

IMAGE_NAME="${!IMAGE_ENV_NAME:-${DEFAULT_IMAGE_NAME}}"
BUILD_ID="${WORKER_BUILD_ID:-$(date +%Y%m%d%H%M%S)}"
ENV_FILE="${ROOT_DIR}/.local/worker_local.env"
DOCKER_BUILDKIT_VALUE="${DOCKER_BUILDKIT:-0}"
MICROK8S_STATUS_CMD="${MICROK8S_STATUS_CMD:-microk8s status --wait-ready}"
MICROK8S_CTR_CMD="${MICROK8S_CTR_CMD:-microk8s ctr}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing required command: $1" >&2
    exit 1
  fi
}

require_cmd docker
require_cmd microk8s

if ! bash -lc "${MICROK8S_STATUS_CMD}" >/dev/null; then
  echo "failed to query microk8s status" >&2
  echo "override with MICROK8S_STATUS_CMD if your environment needs a different command" >&2
  exit 1
fi

if ! bash -lc "${MICROK8S_CTR_CMD} images ls" >/dev/null 2>&1; then
  echo "failed to access microk8s container runtime" >&2
  echo "set MICROK8S_CTR_CMD to a working command, for example: sudo microk8s ctr" >&2
  exit 1
fi

echo "Building ${IMAGE_NAME}"
DOCKER_BUILDKIT="${DOCKER_BUILDKIT_VALUE}" docker build \
  -f "${DOCKERFILE_PATH}" \
  -t "${IMAGE_NAME}" \
  "${ROOT_DIR}"

echo "Importing ${IMAGE_NAME} into microk8s"
docker save "${IMAGE_NAME}" | bash -lc "${MICROK8S_CTR_CMD} image import -"

mkdir -p "$(dirname "${ENV_FILE}")"
cat > "${ENV_FILE}" <<EOF
export SESSION_RUNTIME_MODE=local_microk8s
export WORKER_BUILD_ID=${BUILD_ID}
export SESSION_REMOTE_CONFIG_PATH=${ROOT_DIR}/deploy/k8s/remote-config.yaml
EOF

if [[ "${TARGET}" == "pro" ]]; then
  cat >> "${ENV_FILE}" <<EOF
export LOCAL_IMAGE_NAME_CODER_PRO=${IMAGE_NAME}
EOF
else
  cat >> "${ENV_FILE}" <<EOF
export LOCAL_IMAGE_NAME_CODER_NOOB=${IMAGE_NAME}
EOF
fi

echo "Wrote ${ENV_FILE}"
echo "Build ID: ${BUILD_ID}"
echo "Run: source ${ENV_FILE}"

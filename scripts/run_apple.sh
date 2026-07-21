#!/usr/bin/env bash
set -euo pipefail

ENV_NAME="${ENV_NAME:-apple}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-49913}"
API_KEY="${APPLE_API_KEY:-}"
ANSYS_EXE="${ANSYS_EXE:-C:\\Program Files\\ANSYS Inc\\v232\\ansys\\bin\\winx64\\ANSYS232.exe}"
ANSYS_NP="${ANSYS_NP:-2}"
CERT_FILE=""
KEY_FILE=""
RELOAD=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reload) RELOAD="--reload"; shift ;;
    --host) HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --api-key) API_KEY="$2"; shift 2 ;;
    --certfile) CERT_FILE="$2"; shift 2 ;;
    --keyfile) KEY_FILE="$2"; shift 2 ;;
    --ansys-exe) ANSYS_EXE="$2"; shift 2 ;;
    --ansys-np) ANSYS_NP="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if ! command -v conda >/dev/null 2>&1; then
  for candidate in \
    "$HOME/miniconda3/bin/conda" \
    "$HOME/anaconda3/bin/conda" \
    "/opt/miniconda3/bin/conda" \
    "/opt/anaconda3/bin/conda"; do
    if [[ -x "$candidate" ]]; then
      CONDA="$candidate"
      break
    fi
  done
else
  CONDA="$(command -v conda)"
fi

if [[ -z "${CONDA:-}" ]]; then
  echo "conda를 찾을 수 없습니다. Miniconda/Anaconda를 설치하거나 conda를 PATH에 추가하세요." >&2
  exit 1
fi

if ! [[ "$ANSYS_NP" =~ ^[1-9][0-9]*$ ]]; then
  echo "ANSYS_NP는 양의 정수여야 합니다." >&2
  exit 1
fi
export ANSYS_EXE ANSYS_NP
echo "Using conda: $CONDA"
echo "Using ANSYS: $ANSYS_EXE (-np $ANSYS_NP)"

if "$CONDA" env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
  echo "Conda env '$ENV_NAME' already exists. Updating dependencies..."
  "$CONDA" env update -n "$ENV_NAME" -f environment.yml --prune
else
  echo "Creating conda env '$ENV_NAME' from environment.yml..."
  "$CONDA" env create -f environment.yml
fi

if [[ -n "$API_KEY" ]]; then
  export APPLE_API_KEY="$API_KEY"
  echo "API key protection: enabled (X-API-Key required)"
else
  echo "API key protection: disabled (set --api-key or APPLE_API_KEY to enable)"
fi

UVICORN_ARGS=(python -m uvicorn app:app --host "$HOST" --port "$PORT" --log-level info --access-log)
if [[ -n "$CERT_FILE" && -n "$KEY_FILE" ]]; then
  UVICORN_ARGS+=(--ssl-certfile "$CERT_FILE" --ssl-keyfile "$KEY_FILE")
  SCHEME="https"
else
  SCHEME="http"
fi
if [[ -n "$RELOAD" ]]; then
  UVICORN_ARGS+=("$RELOAD")
fi

echo
echo "Starting APPLE API on $SCHEME://$HOST:$PORT"
echo "Swagger UI: $SCHEME://$HOST:$PORT/docs"
echo "OpenAPI:    $SCHEME://$HOST:$PORT/openapi.json"
echo "Press Ctrl+C to stop."
echo
"$CONDA" run -n "$ENV_NAME" --no-capture-output "${UVICORN_ARGS[@]}"

#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python_cmd="${PYTHON:-python3}"
venv_python="$repo_root/.venv/bin/python"
edgeone_root="$repo_root/adapters/edgeone"
env_file="$edgeone_root/.env"
marker="$repo_root/.venv/.local-prod-setup-v1"
edgeone_marker="$repo_root/.venv/.local-prod-edgeone-v1"
setup_version="v1"

if ! command -v "$python_cmd" >/dev/null 2>&1; then
  echo "Python 3 is required to prepare local-prod." >&2
  exit 1
fi

python_ready() {
  [[ -x "$venv_python" ]] && "$venv_python" -c \
    'import sys; assert sys.version_info >= (3, 11); import fastapi, httpx, uvicorn, mijia_agent' \
    >/dev/null 2>&1
}

linked_project_ready() {
  "$python_cmd" - "$edgeone_root/.edgeone/project.json" <<'PY'
import json
import sys
from pathlib import Path

try:
    project = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
raise SystemExit(0 if project.get("Name") == "mijia-agent" else 1)
PY
}

lock_hash="$($python_cmd -c 'import hashlib, pathlib; print(hashlib.sha256(pathlib.Path("requirements-dev.lock").read_bytes()).hexdigest())')"
expected_marker="v1:$lock_hash"
if python_ready \
  && [[ -f "$marker" ]] \
  && [[ "$(cat "$marker")" == "$expected_marker" ]] \
  && [[ -f "$edgeone_marker" ]] \
  && [[ "$(cat "$edgeone_marker")" == "${setup_version}:mijia-agent:production" ]] \
  && linked_project_ready \
  && [[ -f "$env_file" ]]; then
  exit 0
fi

if ! python_ready || [[ "$(cat "$marker" 2>/dev/null || true)" != "$expected_marker" ]]; then
  if ! "$python_cmd" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
    echo "Python 3.11 or newer is required to prepare local-prod." >&2
    exit 1
  fi
  if [[ -x "$venv_python" ]]; then
    "$python_cmd" -m venv --upgrade .venv
  else
    "$python_cmd" -m venv .venv
  fi
  "$venv_python" -m ensurepip --upgrade >/dev/null
  "$venv_python" -m pip install -r requirements-dev.lock
  "$venv_python" -m pip install --no-deps -e .

  if ! command -v npm >/dev/null 2>&1; then
    echo "npm is required to build the EdgeOne adapter." >&2
    exit 1
  fi
  npm run build --prefix adapters/edgeone
  printf '%s\n' "$expected_marker" > "$marker"
  chmod 600 "$marker"
fi

if ! command -v edgeone >/dev/null 2>&1; then
  echo "EdgeOne CLI is required. Install it and log in, then rerun local-prod." >&2
  exit 1
fi
if ! linked_project_ready; then
  (cd "$edgeone_root" && edgeone makers link --name mijia-agent)
fi

edgeone_state="${setup_version}:mijia-agent:production"
if [[ "$(cat "$edgeone_marker" 2>/dev/null || true)" != "$edgeone_state" || ! -f "$env_file" ]]; then
  temp_env="$(mktemp "$edgeone_root/.env.local-prod.XXXXXX")"
  # Let the CLI's environment picker select the project's actual production
  # environment slug. Filtering with `--environment production` can miss
  # projects whose production environment uses a custom slug.
  if ! (cd "$edgeone_root" && edgeone makers env pull --file "$temp_env"); then
    rm -f "$temp_env"
    echo "Could not pull the production EdgeOne environment; verify the project link and CLI login." >&2
    exit 1
  fi
  if [[ ! -s "$temp_env" ]]; then
    rm -f "$temp_env"
    echo "The production EdgeOne environment pull returned an empty file." >&2
    exit 1
  fi
  chmod 600 "$temp_env"
  mv -f "$temp_env" "$env_file"
  printf '%s\n' "$edgeone_state" > "$edgeone_marker"
  chmod 600 "$edgeone_marker"
fi
chmod 600 "$env_file"

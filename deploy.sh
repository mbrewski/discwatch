#!/usr/bin/env bash
# Deploy this tree to LXC 106 web (discwatch.mattbrewer.me).
# Does not overwrite remote data/ (sqlite, secret_key, smtp.env) unless --with-data.
set -euo pipefail

HOST="${DISCWATCH_HOST:-mbrewski@10.145.22.63}"
REMOTE="${DISCWATCH_REMOTE:-develop/discwatch}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
WITH_DATA=0
SKIP_DEPS=0
HEALTH_URL="http://127.0.0.1:8790/login"

usage() {
  cat <<EOF
Usage: $0 [--with-data] [--skip-deps] [--host USER@HOST] [--remote DIR]

  --with-data   also copy data/ (sqlite, secret_key, smtp.env). Overwrites the live DB.
  --skip-deps   skip remote pip install -r requirements.txt
  --host        default mbrewski@10.145.22.63 (or DISCWATCH_HOST)
  --remote      default develop/discwatch (or DISCWATCH_REMOTE)

Never copies venv/ or __pycache__. Restarts discwatch.service on the target.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-data) WITH_DATA=1; shift ;;
    --skip-deps) SKIP_DEPS=1; shift ;;
    --host) HOST="$2"; shift 2 ;;
    --remote) REMOTE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
done

RSYNC=(rsync -a --delete
  --exclude venv/
  --exclude __pycache__/
  --exclude '*.py[cod]'
  --exclude .pytest_cache/
  --exclude '*.egg-info/'
  --exclude .git/
)
if [[ "$WITH_DATA" -eq 0 ]]; then
  RSYNC+=(--exclude data/)
fi

echo "deploy discwatch -> ${HOST}:${REMOTE}"
"${RSYNC[@]}" "${ROOT}/" "${HOST}:${REMOTE}/"

ssh -o BatchMode=yes "$HOST" env \
  REMOTE="$REMOTE" SKIP_DEPS="$SKIP_DEPS" HEALTH_URL="$HEALTH_URL" \
  bash -s <<'EOS'
set -euo pipefail
cd "$HOME/$REMOTE"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
if [[ ! -x venv/bin/python ]]; then
  python3 -m venv venv
fi
if [[ "${SKIP_DEPS}" -eq 0 ]]; then
  venv/bin/pip install -q -U pip
  venv/bin/pip install -q -r requirements.txt
fi
mkdir -p "$HOME/.config/systemd/user"
cp -f systemd/discwatch.service systemd/discwatch-prices.service systemd/discwatch-releases.service \
  systemd/discwatch-prices.timer systemd/discwatch-releases.timer \
  "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now discwatch.service discwatch-releases.timer discwatch-prices.timer
systemctl --user restart discwatch.service
systemctl --user --quiet is-active discwatch.service
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS --max-time 5 -o /dev/null "$HEALTH_URL"; then
    curl -fsS --max-time 5 -o /dev/null -w "health:%{http_code}\n" "$HEALTH_URL"
    exit 0
  fi
  sleep 0.4
done
echo "discwatch did not answer ${HEALTH_URL}" >&2
systemctl --user --no-pager --full status discwatch.service | tail -20 >&2
exit 1
EOS

echo "discwatch deployed"

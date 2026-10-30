#!/usr/bin/env bash
# cgbot 部署脚本(M5)。在开发机运行;构建与启动均经 SSH 在宿主机执行。
# 只操作 compose 项目 cgbot 的 cgbot 服务与镜像 cgbot:*;不触碰其它容器、网络、卷。
#
# 用法:
#   deploy/deploy.sh --check             只读检查本地与远端前置条件,不改动任何东西
#   deploy/deploy.sh --build             同步源码到远端构建目录并构建镜像(不启动)
#   deploy/deploy.sh --up                检查 + 构建 + 更新 compose 文件 + up -d cgbot + 等待 healthy
#   deploy/deploy.sh --rollback <tag>    切换到已存在的镜像 tag 并重建 cgbot
#
# 可选环境变量:
#   CGBOT_HOST             SSH 目标,默认 klp@192.168.1.19
#   CGBOT_BUILD_DIR        远端构建目录,默认 /home/klp/cgbot-build/bot
#   CGBOT_RUN_DIR          远端运行目录,默认 /home/klp/cgbot-run
#   CGBOT_HOST_DATA_DIR    沙盒工作区宿主机目录,默认 /home/klp/cgbot-data
#   CGBOT_HEALTH_TIMEOUT   等待 healthy 的秒数,默认 180
#
# 本脚本不创建目录、不写 config.toml/secrets.toml、不改 NapCat 与旧 dsh 项目。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${CGBOT_HOST:-klp@192.168.1.19}"
BUILD_DIR="${CGBOT_BUILD_DIR:-/home/klp/cgbot-build/bot}"
RUN_DIR="${CGBOT_RUN_DIR:-/home/klp/cgbot-run}"
DATA_DIR="${CGBOT_HOST_DATA_DIR:-/home/klp/cgbot-data}"
HEALTH_TIMEOUT="${CGBOT_HEALTH_TIMEOUT:-180}"

die() { echo "deploy: $*" >&2; exit 1; }
log() { echo "deploy: $*"; }
ssh_run() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "$@"; }

check_local() {
  command -v ssh >/dev/null || die "ssh is required"
  command -v rsync >/dev/null || die "rsync is required"
  local f
  for f in Dockerfile .dockerignore uv.lock pyproject.toml deploy/docker-compose.yml deploy/config.toml; do
    [[ -f "$ROOT/$f" ]] || die "missing $f"
  done
}

# Read-only remote checks. Never prints secret values.
remote_check() {
  ssh_run bash -s -- "$RUN_DIR" "$DATA_DIR" <<'REMOTE'
set -u
RUN_DIR=$1; DATA_DIR=$2; fail=0
ok()  { echo "ok: $*"; }
bad() { echo "MISSING: $*"; fail=1; }
docker info >/dev/null 2>&1 && ok "docker daemon reachable" || bad "docker daemon not reachable"
docker compose version >/dev/null 2>&1 && ok "docker compose available" || bad "docker compose plugin"
for n in dsh_caged_internal dsh_caged_ctl qqbot_astrbot_network; do
  docker network inspect "$n" >/dev/null 2>&1 && ok "network $n" || bad "network $n (owned by legacy dsh project)"
done
if proj=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' cgbot 2>/dev/null); then
  [ "$proj" = cgbot ] && ok "container cgbot belongs to compose project cgbot" \
    || bad "container named cgbot exists outside project cgbot; refusing to touch it"
else
  ok "no container named cgbot yet"
fi
[ -d "$DATA_DIR" ] && ok "host data dir $DATA_DIR" || bad "host data dir $DATA_DIR (create manually)"
[ -d "$RUN_DIR/state" ] && ok "state dir" || bad "$RUN_DIR/state (create manually)"
if [ -f "$RUN_DIR/config.toml" ]; then
  ok "config.toml present"
  if grep -q '【占位' "$RUN_DIR/config.toml"; then bad "config.toml still has 【占位】 entries"; else ok "config.toml has no 【占位】 entries"; fi
else
  bad "$RUN_DIR/config.toml"
fi
if [ -f "$RUN_DIR/secrets.toml" ]; then
  mode=$(stat -c %a "$RUN_DIR/secrets.toml")
  [ "$mode" = 600 ] && ok "secrets.toml mode 600" || bad "secrets.toml mode $mode (must be 600)"
else
  bad "$RUN_DIR/secrets.toml"
fi
[ "$fail" -eq 0 ] && echo "remote check passed" || { echo "remote check failed"; exit 1; }
REMOTE
}

# Prints the new image tag as its last line; all other output goes to stdout/stderr normally.
build() {
  local rev tag
  rev="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo nogit)"
  tag="$(date -u +%Y%m%d%H%M%S)-$rev"
  ssh_run "mkdir -p '$BUILD_DIR'"
  rsync -a --delete \
    --exclude='/.git' --exclude='/.venv' --exclude='/data' --exclude='/tests' --exclude='/docs' \
    --exclude='/sandbox' --exclude='/deploy' --exclude='/config.toml' --exclude='secrets*' \
    --exclude='*.sqlite*' --exclude='__pycache__' --exclude='.ruff_cache' --exclude='.pytest_cache' \
    "$ROOT/" "$HOST:$BUILD_DIR/" >&2
  ssh_run "cd '$BUILD_DIR' && docker build --build-arg CGBOT_REVISION='$rev' -t 'cgbot:$tag' -t cgbot:latest ." >&2
  log "built cgbot:$tag" >&2
  printf '%s\n' "$tag"
}

# Shared by --up and --rollback: writes non-secret CGBOT_TAG to the run dir .env, then recreates
# only the cgbot service. The compose file is copied from the repo each time.
up_tag() {
  local tag="$1"
  rsync -a "$ROOT/deploy/docker-compose.yml" "$HOST:$RUN_DIR/docker-compose.yml"
  ssh_run "printf 'CGBOT_TAG=%s\nHOST_DATA_DIR=%s\n' '$tag' '$DATA_DIR' > '$RUN_DIR/.env' && cd '$RUN_DIR' && docker compose -p cgbot -f docker-compose.yml up -d --no-deps cgbot"
  wait_healthy
}

wait_healthy() {
  local deadline status
  deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  while :; do
    status="$(ssh_run "docker inspect -f '{{.State.Health.Status}}' cgbot 2>/dev/null || echo missing")"
    case "$status" in
      healthy) log "cgbot healthy"; return 0 ;;
      unhealthy) die "cgbot unhealthy. Inspect: ssh $HOST docker logs --tail 100 cgbot ; roll back: $0 --rollback <previous tag>" ;;
    esac
    (( $(date +%s) > deadline )) && die "timeout waiting for healthy (last status: $status)"
    sleep 5
  done
}

case "${1:-}" in
  --check)
    check_local
    remote_check
    log "check passed"
    ;;
  --build)
    check_local
    build >/dev/null
    ;;
  --up)
    check_local
    remote_check
    tag="$(build | tail -n1)"
    up_tag "$tag"
    ;;
  --rollback)
    [[ $# -eq 2 && "$2" =~ ^[A-Za-z0-9._-]+$ ]] || die "usage: $0 --rollback <tag>"
    check_local
    ssh_run "docker image inspect 'cgbot:$2' >/dev/null 2>&1" || die "image cgbot:$2 not found on $HOST"
    up_tag "$2"
    ;;
  *)
    sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac

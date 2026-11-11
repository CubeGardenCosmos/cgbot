#!/usr/bin/env bash
# cgbot 部署脚本(M5)。在开发机运行;构建与启动均经 SSH 在宿主机执行。
# 只操作 compose 项目 cgbot 的 cgbot 服务与镜像 cgbot:*;不触碰其它容器、网络、卷。
#
# 用法:
#   deploy/deploy.sh --check             只读检查本地与远端前置条件,不改动任何东西
#   deploy/deploy.sh --build             同步源码到远端构建目录并构建镜像(不启动)
#   deploy/deploy.sh --caged-up          同步 caged/ 到远端并启动 caged compose stack
#   deploy/deploy.sh --up                检查 + 构建 + 更新 compose 文件 + up -d cgbot + 等待 healthy
#   deploy/deploy.sh --rollback <tag>    切换到已存在的镜像 tag 并重建 cgbot
#
# 必需环境变量:
#   CGBOT_HOST             SSH 目标(必需,无默认值)
# 可选环境变量:
#   CGBOT_BUILD_DIR        远端构建目录,默认 $HOME/cgbot-build/bot
#   CGBOT_RUN_DIR          远端运行目录,默认 $HOME/cgbot-run
#   CGBOT_CAGED_DIR        远端 caged/ 目录,默认 $HOME/caged
#   CGBOT_HOST_DATA_DIR    沙盒工作区宿主机目录,默认 $HOME/cgbot-data
#   CGBOT_HEALTH_TIMEOUT   等待 healthy 的秒数,默认 180
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HEALTH_TIMEOUT="${CGBOT_HEALTH_TIMEOUT:-180}"
HOST="${CGBOT_HOST:-}"

die() { echo "deploy: $*" >&2; exit 1; }
log() { echo "deploy: $*"; }
ssh_run() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "$@"; }

# Requires CGBOT_HOST; resolves the remote $HOME so defaults are absolute remote paths.
init_env() {
  [[ -n "$HOST" ]] || die "CGBOT_HOST is required (e.g. user@host)"
  local rhome
  rhome="$(ssh_run 'printf %s "$HOME"')" || die "cannot ssh to $HOST"
  BUILD_DIR="${CGBOT_BUILD_DIR:-$rhome/cgbot-build/bot}"
  RUN_DIR="${CGBOT_RUN_DIR:-$rhome/cgbot-run}"
  CAGED_DIR="${CGBOT_CAGED_DIR:-$rhome/caged}"
  DATA_DIR="${CGBOT_HOST_DATA_DIR:-$rhome/cgbot-data}"
}

check_local() {
  command -v ssh >/dev/null || die "ssh is required"
  command -v rsync >/dev/null || die "rsync is required"
  local f
  for f in Dockerfile .dockerignore uv.lock pyproject.toml deploy/docker-compose.yml deploy/config.toml; do
    [[ -f "$ROOT/$f" ]] || die "missing $f"
  done
  init_env
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
for n in caged_internal caged_ctl cgbot_net; do
  docker network inspect "$n" >/dev/null 2>&1 && ok "network $n" || bad "network $n (owned by caged/ or create manually)"
done
for svc in caged-docker-proxy caged-egress; do
  docker inspect --type container "$svc" >/dev/null 2>&1 && ok "service $svc running" || bad "service $svc not running (start caged/ stack)"
done
if proj=$(docker inspect --type container -f '{{index .Config.Labels "com.docker.compose.project"}}' cgbot 2>/dev/null); then
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
  ssh_run "printf 'CGBOT_TAG=%s\nCGBOT_HOST_DATA_DIR=%s\n' '$tag' '$DATA_DIR' > '$RUN_DIR/.env' && cd '$RUN_DIR' && docker compose -p cgbot -f docker-compose.yml up -d --no-deps cgbot"
  wait_healthy
}

wait_healthy() {
  local deadline status
  deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  while :; do
    status="$(ssh_run "docker inspect --type container -f '{{.State.Health.Status}}' cgbot 2>/dev/null || echo missing")"
    case "$status" in
      healthy) log "cgbot healthy"; return 0 ;;
      unhealthy) die "cgbot unhealthy. Inspect: ssh $HOST docker logs --tail 100 cgbot ; roll back: $0 --rollback <previous tag>" ;;
    esac
    (( $(date +%s) > deadline )) && die "timeout waiting for healthy (last status: $status)"
    sleep 5
  done
}

caged_up() {
  [[ -d "$ROOT/caged" ]] || die "caged/ directory not found in repo"
  log "syncing caged/ to $HOST:$CAGED_DIR"
  ssh_run "mkdir -p '$CAGED_DIR'"
  rsync -a --delete --exclude='/.env' "$ROOT/caged/" "$HOST:$CAGED_DIR/"
  ssh_run "test -f '$CAGED_DIR/.env'" || die "$CAGED_DIR/.env missing on $HOST: create it from caged/.env.example (never synced) and rerun"
  log "starting caged stack"
  ssh_run "cd '$CAGED_DIR' && docker compose -p caged up -d"
  log "caged stack started"
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
  --caged-up)
    check_local
    caged_up
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
    sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac

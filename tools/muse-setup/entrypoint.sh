#!/bin/sh
# muse-setup 入口:检查 machine-id,启动 Xvfb 与会话总线,运行驱动脚本。
set -eu

if [ ! -s /etc/machine-id ]; then
  echo "错误: /etc/machine-id 为空或未挂载。请挂载你持久保存的 machine-id 文件(只读),见 README.md。" >&2
  exit 2
fi
if [ -w /etc/machine-id ] 2>/dev/null && [ "$(id -u)" -eq 0 ]; then
  echo "警告: /etc/machine-id 可写,建议以 :ro 挂载。" >&2
fi

mkdir -p /state/.local/share /samples /tmp/.X11-unix 2>/dev/null || true

Xvfb :99 -screen 0 1280x900x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!
trap 'kill "$XVFB_PID" 2>/dev/null || true' EXIT INT TERM

i=0
while [ ! -S /tmp/.X11-unix/X99 ]; do
  i=$((i + 1))
  if [ "$i" -gt 100 ]; then
    echo "错误: Xvfb 未在 10 秒内启动,日志见 /tmp/xvfb.log:" >&2
    cat /tmp/xvfb.log >&2 || true
    exit 3
  fi
  sleep 0.1
done

set +e
dbus-run-session -- python3 -u /usr/local/bin/muse_setup.py "$@"
rc=$?
exit "$rc"

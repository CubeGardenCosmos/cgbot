#!/bin/sh
# 在宿主机上创建持久的 machine-id 文件(已存在则不动)。
# 用法: tools/muse-setup/ensure-machine-id.sh [路径]    默认 ~/.local/share/cgbot/muse/machine-id
# 这个文件之后要以只读方式挂到 muse-setup 容器和所有渲染容器的 /etc/machine-id。
# 一旦用它生成过 receipt,就不要再换、不要删:receipt 与它绑定。
set -eu

path=${1:-"$HOME/.local/share/cgbot/muse/machine-id"}

if [ -s "$path" ]; then
  echo "已存在,保持不变: $path"
  exit 0
fi

mkdir -p "$(dirname "$path")"
# 32 位小写十六进制,与 systemd machine-id 格式一致
id=$(tr -d '-' < /proc/sys/kernel/random/uuid)
if [ "${#id}" -ne 32 ]; then
  echo "错误: 生成 machine-id 失败" >&2
  exit 1
fi
umask 022
printf '%s\n' "$id" > "$path"
chmod 0444 "$path"
echo "已创建: $path"
echo "请备份该文件;之后一直以 -v '$path':/etc/machine-id:ro 挂载。"

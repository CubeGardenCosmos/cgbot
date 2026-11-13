#!/usr/bin/env bash
# 开发期辅助脚本(仅本地开发,不进入镜像与部署,不提交)。
#
# 1. 从 MuseHub 官方地址下载 Muse Sounds Manager Linux DEB 包,校验 sha256 后解包到 OUTDIR/pkg。
# 2. 可选:若设置 MUSESAMPLER_SOURCE_LIB,把用户本人经官方登录/安装流程取得的
#    libMuseSamplerCoreLib.so 复制到 OUTDIR/lib,并记录 sha256、readelf -d 与 ms_* 导出。
#    脚本不登录、不下载 libMuseSamplerCoreLib.so、不绕过任何授权流程。
# 3. 写 OUTDIR/MANIFEST.txt 清单。
#
# 约束:
# - 拒绝以 root 运行;
# - OUTDIR 与 MUSESAMPLER_SOURCE_LIB 均不得位于仓库内;
# - 所有输出只写入 OUTDIR,不写仓库树。
#
# 用法:
#   scripts/extract_musesampler.sh OUTDIR
#
# 环境变量(均可选):
#   MUSESAMPLER_DEB_URL     DEB 下载地址(默认 https://muse-cdn.com/Muse_Sounds_Manager_x64.deb)
#   MUSESAMPLER_DEB_SHA256  期望 sha256(默认为 2026-11 实测值 53209132…072813)
#   MUSESAMPLER_SOURCE_LIB  用户已取得的 libMuseSamplerCoreLib.so 路径(可选)
#
# 幂等:DEB 已存在且 sha256 一致则跳过下载;pkg 已由同一 sha256 解包则跳过解包;
# 库文件每次重新登记(复制 + 记录),结果相同。

set -euo pipefail

log() { printf '[musesampler] %s\n' "$*"; }
die() { printf '错误: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
用法: scripts/extract_musesampler.sh OUTDIR
OUTDIR 输出目录(不得位于仓库内)。下载、解包、库登记都发生在这里。
EOF
}

if [ "$(id -u)" -eq 0 ]; then
  die "禁止以 root 运行,请使用普通用户"
fi

if [ $# -ne 1 ]; then
  usage
  exit 2
fi
case "$1" in
  -h|--help) usage; exit 0 ;;
esac

OUT=$(realpath -m -- "$1")
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)

if [ -n "$REPO" ]; then
  case "$OUT/" in
    "$REPO"/*) die "OUTDIR 位于仓库内($REPO),拒绝写入: $OUT" ;;
  esac
fi

command -v curl >/dev/null || die "缺少 curl"
command -v ar >/dev/null || die "缺少 ar(binutils)"
command -v tar >/dev/null || die "缺少 tar"
command -v sha256sum >/dev/null || die "缺少 sha256sum"
command -v readelf >/dev/null || die "缺少 readelf(binutils)"
command -v nm >/dev/null || die "缺少 nm(binutils)"
python3 -c 'import compression.zstd' 2>/dev/null \
  || die "需要 Python 3.14+ 的 compression.zstd(系统无 zstd 时使用)"

DEB_URL=${MUSESAMPLER_DEB_URL:-https://muse-cdn.com/Muse_Sounds_Manager_x64.deb}
EXPECTED_SHA=${MUSESAMPLER_DEB_SHA256:-53209132a814c49c6d7c61e1ab28e99adf09eb40a7fdde8f2eeb3f03cf072813}

sha_of() { sha256sum -- "$1" | cut -d' ' -f1; }

mkdir -p "$OUT/download"
DEB="$OUT/download/Muse_Sounds_Manager_x64.deb"

if [ -f "$DEB" ] && [ "$(sha_of "$DEB")" = "$EXPECTED_SHA" ]; then
  log "DEB 已存在且 sha256 一致,跳过下载: $DEB"
else
  log "下载 $DEB_URL"
  curl --fail --location --retry 2 --output "$DEB.part" "$DEB_URL"
  got=$(sha_of "$DEB.part")
  if [ "$got" != "$EXPECTED_SHA" ]; then
    rm -f -- "$DEB.part"
    die "sha256 不符: got $got expected $EXPECTED_SHA"
  fi
  mv -f -- "$DEB.part" "$DEB"
fi

PKG="$OUT/pkg"
STAMP="$PKG/.deb-sha256"
if [ -f "$STAMP" ] && [ "$(cat -- "$STAMP")" = "$EXPECTED_SHA" ]; then
  log "已解包同一版本,跳过: $PKG"
else
  WORK="$OUT/.work"
  rm -rf -- "$WORK" "$PKG"
  mkdir -p -- "$WORK" "$PKG"
  (cd "$WORK" && ar x "$DEB")
  [ -f "$WORK/data.tar.zst" ] || die "DEB 中缺少 data.tar.zst"
  python3 -c '
import shutil, sys
from compression import zstd
with zstd.open(sys.argv[1], "rb") as f:
    shutil.copyfileobj(f, sys.stdout.buffer)
' "$WORK/data.tar.zst" | tar -x -f - -C "$PKG"
  printf '%s\n' "$EXPECTED_SHA" > "$STAMP"
  rm -rf -- "$WORK"
  log "已解包到 $PKG"
fi

LIB_DIR="$OUT/lib"
LIB_STATUS="未提供 MUSESAMPLER_SOURCE_LIB,未获得 libMuseSamplerCoreLib.so(见 docs/reference/musesampler.md §10 第 4 步)"

if [ -n "${MUSESAMPLER_SOURCE_LIB:-}" ]; then
  src=$(realpath -e -- "$MUSESAMPLER_SOURCE_LIB") || die "MUSESAMPLER_SOURCE_LIB 路径不存在: $MUSESAMPLER_SOURCE_LIB"
  [ -f "$src" ] || die "MUSESAMPLER_SOURCE_LIB 不是普通文件: $src"
  if [ -n "$REPO" ]; then
    case "$src" in
      "$REPO"/*) die "MUSESAMPLER_SOURCE_LIB 位于仓库内,拒绝: $src" ;;
    esac
  fi
  magic=$(head -c 4 -- "$src" | od -An -c | tr -d ' ')
  [ "$magic" = '177ELF' ] || die "不是 ELF 文件: $src"

  mkdir -p -- "$LIB_DIR"
  LIB="$LIB_DIR/libMuseSamplerCoreLib.so"
  cp -f -- "$src" "$LIB"
  readelf -d "$LIB" > "$LIB_DIR/readelf-d.txt"
  nm -D --defined-only "$LIB" | sed -n 's/.* \(ms_[A-Za-z0-9_]*\)$/\1/p' | sort -u > "$LIB_DIR/ms_exports.txt"
  n=$(wc -l < "$LIB_DIR/ms_exports.txt")
  LIB_STATUS="已登记 $LIB sha256=$(sha_of "$LIB") ms_* 导出数=$n"
  log "$LIB_STATUS"
fi

{
  echo "URL: $DEB_URL"
  echo "DEB: $DEB"
  echo "DEB size: $(stat -c %s -- "$DEB") bytes"
  echo "DEB sha256: $(sha_of "$DEB")"
  echo "DEB sha256 expected: $EXPECTED_SHA"
  echo "pkg: $PKG"
  echo "library: $LIB_STATUS"
} > "$OUT/MANIFEST.txt"

log "完成。清单: $OUT/MANIFEST.txt"

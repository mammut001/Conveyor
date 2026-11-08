#!/usr/bin/env bash
# install_pi.sh — 安装随 Conveyor 附带的 Pi 编码代理到 vendor/pi（幂等，不装全局）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PI_DIR="${ROOT}/vendor/pi"
PI_BIN="${PI_DIR}/node_modules/.bin/pi"
MIN_MAJOR=22
MIN_MINOR=19

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "错误：未找到 node / npm。Pi 需要 Node.js >= 22.19.0，请先安装后重试。" >&2
  exit 1
fi

NODE_VERSION="$(node -v | sed 's/^v//')"
NODE_MAJOR="${NODE_VERSION%%.*}"
REST="${NODE_VERSION#*.}"
NODE_MINOR="${REST%%.*}"
if (( NODE_MAJOR < MIN_MAJOR )) || { (( NODE_MAJOR == MIN_MAJOR )) && (( NODE_MINOR < MIN_MINOR )); }; then
  echo "错误：当前 Node.js 版本为 v${NODE_VERSION}，Pi 需要 >= 22.19.0，请升级 Node.js 后重试。" >&2
  exit 1
fi

[[ -f "${PI_DIR}/package-lock.json" ]] || { echo "错误：缺少 ${PI_DIR}/package-lock.json" >&2; exit 1; }

cd "${PI_DIR}"
echo "[install_pi] 在 ${PI_DIR} 安装 Pi（npm ci --omit=dev --ignore-scripts）..."
npm ci --omit=dev --ignore-scripts --no-audit --no-fund

if [[ ! -x "${PI_BIN}" ]]; then
  echo "错误：安装完成但未找到 ${PI_BIN}" >&2
  exit 1
fi
echo "[install_pi] 完成：$("${PI_BIN}" --version 2>/dev/null || echo 'pi')"

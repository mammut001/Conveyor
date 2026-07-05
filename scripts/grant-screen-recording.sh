#!/usr/bin/env bash
# Open Screen Recording settings and reveal capture-screen-helper in Finder.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$HERE/.desktop-agent.env"

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

HELPER="${CONVEYOR_DESKTOP_SCREENSHOT_HELPER:-}"
if [[ -z "$HELPER" || ! -x "$HELPER" ]]; then
  echo "未找到可执行的 capture-screen-helper。"
  echo "请先运行: bash scripts/setup-desktop-agent.sh"
  exit 1
fi

echo "截图工具: $HELPER"
echo "正在打开系统设置 → 屏幕录制，并在 Finder 中定位 helper..."
open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
open -R "$HELPER"
printf '%s' "$HELPER" | pbcopy

cat <<'EOF'

下一步：
1. 在系统设置点击 +，选择 Finder 里高亮的 capture-screen-helper
2. 打开开关；若提示退出应用，选「退出并重新打开」
3. 菜单栏 Conveyor Agent → Restart All
4. 在飞书重试：截图看看我电脑现在是什么

helper 路径已复制到剪贴板。
EOF
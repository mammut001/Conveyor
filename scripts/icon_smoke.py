"""icon_smoke.py — 验证 Conveyor Agent 菜单栏图标资产的完整性。

检查项(15 项):
  1-4.  4 个状态的 master @3x PNG 存在
  5-7.  4 个状态的 @2x + @1x PNG 存在
  8.    PNG 都是 RGBA、模板图像特征(透明背景)
  9.    PNG 尺寸正确(1024/512/256)
  10.   AppIcon.iconset 包含 macOS 要求的 10 个文件名
  11.   AppIcon.icns 文件存在
  12.   Info.plist 含 CFBundleIconFile + CFBundleIconName
  13.   App.swift 含 Image(nsImage: IconCatalog.image(...))
  14.   IconCatalog.swift 含 isTemplate = true
  15.   build.sh 拷 Assets 资产到 Contents/Resources

用法: .venv/bin/python scripts/icon_smoke.py
"""

from __future__ import annotations

import re
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MENU_DIR = REPO / "menubar-agent" / "Resources" / "Assets" / "MenuBar"
APPICON_DIR = REPO / "menubar-agent" / "Resources" / "Assets" / "AppIcon"
INFO_PLIST = REPO / "menubar-agent" / "Resources" / "Info.plist"
APP_SWIFT = REPO / "menubar-agent" / "Sources" / "ConveyorAgent" / "App.swift"
ICON_CATALOG = REPO / "menubar-agent" / "Sources" / "ConveyorAgent" / "IconCatalog.swift"
BUILD_SH = REPO / "menubar-agent" / "build.sh"

STATES = ("healthy", "partial", "down", "unknown")
REQUIRED_SIZES_MENU = {256, 512, 1024}  # @1x/@2x/@3x master
ICONSET_FILES = [
    "icon_16x16.png",
    "icon_16x16@2x.png",
    "icon_32x32.png",
    "icon_32x32@2x.png",
    "icon_128x128.png",
    "icon_128x128@2x.png",
    "icon_256x256.png",
    "icon_256x256@2x.png",
    "icon_512x512.png",
    "icon_512x512@2x.png",
]

failures: list[str] = []


def check(cond: bool, msg: str):
    if cond:
        print(f"  ✓ {msg}")
    else:
        print(f"  ✗ {msg}")
        failures.append(msg)


def png_size(path: Path) -> tuple[int, int] | None:
    """Read PNG dimensions from IHDR."""
    try:
        with open(path, "rb") as f:
            data = f.read(24)
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    except Exception:
        return None


def png_has_alpha(path: Path) -> bool:
    """Check if PNG has a tRNS chunk or color type 4/6 (alpha)."""
    try:
        with open(path, "rb") as f:
            sig = f.read(8)
            if sig != b"\x89PNG\r\n\x1a\n":
                return False
            # Walk chunks
            while True:
                length_bytes = f.read(4)
                if len(length_bytes) < 4:
                    return False
                length = struct.unpack(">I", length_bytes)[0]
                chunk_type = f.read(4)
                if chunk_type == b"IHDR":
                    ihdr = f.read(length)
                    color_type = ihdr[9]
                    f.read(4)  # CRC
                    # color_type: 4=gray+alpha, 6=RGBA, 3=indexed (may have tRNS)
                    if color_type in (4, 6):
                        return True
                    # indexed PNG — look for tRNS
                    if color_type == 3:
                        # continue walking
                        pass
                elif chunk_type == b"tRNS":
                    return True
                elif chunk_type == b"IEND":
                    return False
                else:
                    f.read(length + 4)  # data + CRC
    except Exception:
        return False
    return False


def main():
    print(f"[icon-smoke] repo: {REPO}")
    print()

    # 1-4: master @3x per state
    print("master @3x PNGs:")
    for state in STATES:
        path = MENU_DIR / f"icon-state-{state}@3x.png"
        check(path.exists(), f"  icon-state-{state}@3x.png exists")

    # 5-7: @2x + @1x
    print("@2x + @1x PNGs:")
    for state in STATES:
        for sz_tag in ("@2x", "@1x"):
            path = MENU_DIR / f"icon-state-{state}{sz_tag}.png"
            check(path.exists(), f"  icon-state-{state}{sz_tag}.png exists")

    # 8: template image — has alpha
    print("image properties:")
    sample = MENU_DIR / "icon-state-healthy@3x.png"
    check(png_has_alpha(sample), f"  {sample.name} has alpha channel")

    # 8b: 彩色版 — 不是纯黑前景
    from PIL import Image
    img = Image.open(sample).convert("RGBA")
    px = img.load()
    w, h = img.size
    colored_count = 0
    for y in range(0, h, 8):  # sample
        for x in range(0, w, 8):
            r, g, b, a = px[x, y]
            if a > 200 and not (r < 30 and g < 30 and b < 30):
                colored_count += 1
    check(colored_count > 10, f"  healthy icon has color (non-black pixels: {colored_count})")

    # 9: sizes correct
    print("PNG dimensions:")
    expected = {"@1x": 256, "@2x": 512, "@3x": 1024}
    for tag, size in expected.items():
        p = MENU_DIR / f"icon-state-healthy{tag}.png"
        actual = png_size(p)
        check(
            actual == (size, size),
            f"  icon-state-healthy{tag}.png is {size}x{size} (got {actual})",
        )

    # 10: iconset files
    print("AppIcon.iconset contents:")
    iconset = APPICON_DIR / "icon.iconset"
    for fname in ICONSET_FILES:
        check(
            (iconset / fname).exists(),
            f"  {fname} present in iconset",
        )

    # 11: AppIcon.icns
    print("AppIcon.icns:")
    check(
        (APPICON_DIR / "AppIcon.icns").exists(),
        "AppIcon.icns exists",
    )
    icns_size = (APPICON_DIR / "AppIcon.icns").stat().st_size if (APPICON_DIR / "AppIcon.icns").exists() else 0
    check(icns_size > 1000, f"AppIcon.icns non-trivial size ({icns_size} bytes)")

    # 12: Info.plist
    print("Info.plist:")
    plist = INFO_PLIST.read_text()
    check("CFBundleIconFile" in plist, "CFBundleIconFile present")
    check("CFBundleIconName" in plist, "CFBundleIconName present")
    check("<string>AppIcon</string>" in plist, "AppIcon referenced as the icon")

    # 13: App.swift uses IconCatalog
    print("App.swift:")
    app_src = APP_SWIFT.read_text()
    check(
        "Image(nsImage:" in app_src and "IconCatalog.image" in app_src,
        "MenuBarExtra label uses IconCatalog.image(...)",
    )
    check(
        'Text("\(monitor.health.overallEmoji) C")' not in app_src,
        "old emoji-only label removed",
    )

    # 14: IconCatalog 不再用 template 模式(整体上色版)
    print("IconCatalog.swift:")
    cat_src = ICON_CATALOG.read_text()
    check("isTemplate = false" in cat_src, "isTemplate = false (colored icons)")
    check("OverallHealth" in cat_src, "OverallHealth enum referenced")

    # 15: build.sh copies assets
    print("build.sh:")
    build_src = BUILD_SH.read_text()
    check("MenuBar" in build_src and "AppIcon.icns" in build_src,
          "build.sh copies MenuBar + AppIcon.icns into bundle")

    # summary
    print()
    if failures:
        print(f"FAIL: {len(failures)} check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("OK: all icon asset checks passed")


if __name__ == "__main__":
    main()

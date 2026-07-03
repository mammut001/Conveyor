"""draw_bot.py — 生成 Conveyor Agent 的菜单栏图标 PNG 资产。

设计:一只 cute 的"传送带机器人",整体作为 template image(单色前景 + 透明背景),
macOS NSImage 会按状态着色(绿/红/黄/灰)。

形状构成(都在 1024×1024 画布上画,坐标中心 512):
  · 圆角矩形身体(履带造型)— 主黑块
  · 顶部圆角矩形头部 + 两只圆眼睛(实心黑)+ 高光(留透明)
  · 一对小天线 + 顶端小球
  · 底部两个履带轮子(环形 — 挖空中心)
  · 身体中部两条横纹(挖透明,做"履带槽")
  · 微笑嘴(挖透明)
  · 右侧 C 形缺口(挖透明)— 暗示 Conveyor
  · 一对小腮红(深灰半透明)— 不会随着 tint 失去层次

输出:
  Resources/Assets/MenuBar/icon-state-{healthy,partial,down,unknown}@{1,2,3}x.png
  Resources/Assets/AppIcon/icon_{16,32,64,128,256,512,1024}.png  (用 healthy 状态)

幂等。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# 设计常量
# ---------------------------------------------------------------------------

MASTER = 1024


def s(v: float) -> float:
    return v


# 圆角矩形身体 — 履带造型
# 占满画布,顶到边缘(只留 ~40px padding 给菜单栏自动裁切)
BODY_RECT = (140, 410, 884, 820)
BODY_RADIUS = 130

# 头 — 比身体略小,与身体有明显"脖子"间隔
HEAD_RECT = (250, 180, 774, 430)
HEAD_RADIUS = 120

# 脖子(连接头和身体的细条)
NECK_RECT = (420, 400, 604, 440)
NECK_RADIUS = 20

# 眼睛(实心黑圆)— 在头部中间,放大
EYE_LEFT_CENTER = (395, 305)
EYE_RIGHT_CENTER = (629, 305)
EYE_RADIUS = 62

# 眼睛高光
EYE_HIGHLIGHT_RADIUS = 19
EYE_HIGHLIGHT_OFFSET = (-20, -22)

# 嘴(挖透明)— 头下部,笑弧
MOUTH_CENTER = (512, 365)
MOUTH_RX = 56
MOUTH_RY = 26

# 腮红
BLUSH_LEFT_CENTER = (310, 350)
BLUSH_RIGHT_CENTER = (714, 350)
BLUSH_RX = 36
BLUSH_RY = 20

# 天线
ANTENNA_LEFT_BASE = (370, 182)
ANTENNA_RIGHT_BASE = (654, 182)
ANTENNA_TIP_OFFSET_Y = -78
ANTENNA_TIP_RADIUS = 32
ANTENNA_STEM_WIDTH = 20

# 履带轮子
WHEEL_LEFT_CENTER = (260, 880)
WHEEL_RIGHT_CENTER = (764, 880)
WHEEL_OUTER = 84
WHEEL_INNER = 40

# 传送带横纹
CONVEYOR_SLOT = (200, 575, 824, 625)
SLOT_RADIUS = 22

# C 形缺口 — 身体右中,挖透明
C_NOTCH_CENTER = (910, 615)
C_NOTCH_RADIUS = 85


# ---------------------------------------------------------------------------
# 绘图
# ---------------------------------------------------------------------------


def draw_bot_base(size: int) -> Image.Image:
    scale = size / MASTER
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def sc(v: float) -> int:
        return int(round(v * scale))

    # ---- 履带轮子 ----
    for cx, cy in (WHEEL_LEFT_CENTER, WHEEL_RIGHT_CENTER):
        d.ellipse(
            (sc(cx - WHEEL_OUTER), sc(cy - WHEEL_OUTER),
             sc(cx + WHEEL_OUTER), sc(cy + WHEEL_OUTER)),
            fill=(0, 0, 0, 255),
        )
        # 中心挖空 — 环
        d.ellipse(
            (sc(cx - WHEEL_INNER), sc(cy - WHEEL_INNER),
             sc(cx + WHEEL_INNER), sc(cy + WHEEL_INNER)),
            fill=(0, 0, 0, 0),
        )

    # ---- 身体(圆角矩形)— 主黑块 ----
    d.rounded_rectangle(
        (sc(BODY_RECT[0]), sc(BODY_RECT[1]),
         sc(BODY_RECT[2]), sc(BODY_RECT[3])),
        radius=sc(BODY_RADIUS),
        fill=(0, 0, 0, 255),
    )

    # ---- 脖子(细条)— 连接头和身体 ----
    d.rounded_rectangle(
        (sc(NECK_RECT[0]), sc(NECK_RECT[1]),
         sc(NECK_RECT[2]), sc(NECK_RECT[3])),
        radius=sc(NECK_RADIUS),
        fill=(0, 0, 0, 255),
    )

    # ---- C 形缺口 — 右侧开口(挖透明)----
    d.ellipse(
        (sc(C_NOTCH_CENTER[0] - C_NOTCH_RADIUS),
         sc(C_NOTCH_CENTER[1] - C_NOTCH_RADIUS),
         sc(C_NOTCH_CENTER[0] + C_NOTCH_RADIUS),
         sc(C_NOTCH_CENTER[1] + C_NOTCH_RADIUS)),
        fill=(0, 0, 0, 0),
    )

    # ---- 传送带横纹(挖透明)— 单条 ----
    d.rounded_rectangle(
        (sc(CONVEYOR_SLOT[0]), sc(CONVEYOR_SLOT[1]),
         sc(CONVEYOR_SLOT[2]), sc(CONVEYOR_SLOT[3])),
        radius=sc(SLOT_RADIUS),
        fill=(0, 0, 0, 0),
    )

    # ---- 头部(圆角矩形)----
    d.rounded_rectangle(
        (sc(HEAD_RECT[0]), sc(HEAD_RECT[1]),
         sc(HEAD_RECT[2]), sc(HEAD_RECT[3])),
        radius=sc(HEAD_RADIUS),
        fill=(0, 0, 0, 255),
    )

    # ---- 天线 ----
    for base_x, base_y in (ANTENNA_LEFT_BASE, ANTENNA_RIGHT_BASE):
        tx, ty = base_x, base_y + ANTENNA_TIP_OFFSET_Y
        d.line(
            [(sc(base_x), sc(base_y)), (sc(tx), sc(ty))],
            fill=(0, 0, 0, 255),
            width=sc(ANTENNA_STEM_WIDTH),
        )
        d.ellipse(
            (sc(tx - ANTENNA_TIP_RADIUS), sc(ty - ANTENNA_TIP_RADIUS),
             sc(tx + ANTENNA_TIP_RADIUS), sc(ty + ANTENNA_TIP_RADIUS)),
            fill=(0, 0, 0, 255),
        )

    # ---- 眼睛(实心黑)----
    for ec in (EYE_LEFT_CENTER, EYE_RIGHT_CENTER):
        d.ellipse(
            (sc(ec[0] - EYE_RADIUS), sc(ec[1] - EYE_RADIUS),
             sc(ec[0] + EYE_RADIUS), sc(ec[1] + EYE_RADIUS)),
            fill=(0, 0, 0, 255),
        )

    # ---- 眼睛高光(挖透明)— 左上小圆点 ----
    for ec in (EYE_LEFT_CENTER, EYE_RIGHT_CENTER):
        hx = ec[0] + EYE_HIGHLIGHT_OFFSET[0]
        hy = ec[1] + EYE_HIGHLIGHT_OFFSET[1]
        d.ellipse(
            (sc(hx - EYE_HIGHLIGHT_RADIUS), sc(hy - EYE_HIGHLIGHT_RADIUS),
             sc(hx + EYE_HIGHLIGHT_RADIUS), sc(hy + EYE_HIGHLIGHT_RADIUS)),
            fill=(0, 0, 0, 0),
        )

    # ---- 嘴(挖透明)— 笑弧 ----
    # 做法:画一个椭圆(挖透明),再用一个矩形盖住上半 — 露出下弧
    cx, cy = MOUTH_CENTER
    d.ellipse(
        (sc(cx - MOUTH_RX), sc(cy - MOUTH_RY),
         sc(cx + MOUTH_RX), sc(cy + MOUTH_RY)),
        fill=(0, 0, 0, 0),
    )
    # 用一个矩形盖住椭圆上半(从椭圆顶部到嘴中心线)
    d.rectangle(
        (sc(cx - MOUTH_RX - 2), sc(cy - MOUTH_RY - 2),
         sc(cx + MOUTH_RX + 2), sc(cy)),
        fill=(0, 0, 0, 255),
    )

    # ---- 腮红(深灰半透明)— 装饰,不会因 tint 失去层次 ----
    for bc in (BLUSH_LEFT_CENTER, BLUSH_RIGHT_CENTER):
        d.ellipse(
            (sc(bc[0] - BLUSH_RX), sc(bc[1] - BLUSH_RY),
             sc(bc[0] + BLUSH_RX), sc(bc[1] + BLUSH_RY)),
            fill=(80, 80, 80, 180),  # 半透明灰
        )

    return img


# ---------------------------------------------------------------------------
# 状态变体
# ---------------------------------------------------------------------------


def add_expression(img: Image.Image, state: str) -> Image.Image:
    size = img.size
    scale = size[0] / MASTER
    d = ImageDraw.Draw(img)

    def sc(v: float) -> int:
        return int(round(v * scale))

    if state == "healthy":
        # 默认笑脸 — 无需修改
        pass
    elif state == "partial":
        # 眯眼笑 — 用两个上凸的弧(画两条粗线段带圆角)
        for ec in (EYE_LEFT_CENTER, EYE_RIGHT_CENTER):
            # 删掉原眼睛区域(挖透明)
            d.ellipse(
                (sc(ec[0] - EYE_RADIUS), sc(ec[1] - EYE_RADIUS),
                 sc(ec[0] + EYE_RADIUS), sc(ec[1] + EYE_RADIUS)),
                fill=(0, 0, 0, 0),
            )
            # 画一个上凸的弧线(眯眯眼)— PIL arc start=0 end=180 走的是上弧
            d.arc(
                (sc(ec[0] - EYE_RADIUS), sc(ec[1] - EYE_RADIUS // 2),
                 sc(ec[0] + EYE_RADIUS), sc(ec[1] + EYE_RADIUS // 2)),
                start=0, end=180,
                fill=(0, 0, 0, 255),
                width=sc(14),
            )
        # 嘴变平
        cx, cy = MOUTH_CENTER
        # 先把原笑弧盖回
        d.rectangle(
            (sc(cx - MOUTH_RX - 4), sc(cy - MOUTH_RY - 4),
             sc(cx + MOUTH_RX + 4), sc(cy + 2)),
            fill=(0, 0, 0, 255),
        )
        # 挖平嘴
        d.ellipse(
            (sc(cx - MOUTH_RX * 0.7), sc(cy - 3),
             sc(cx + MOUTH_RX * 0.7), sc(cy + 5)),
            fill=(0, 0, 0, 0),
        )
    elif state == "down":
        # 眼睛画成 X
        for ec in (EYE_LEFT_CENTER, EYE_RIGHT_CENTER):
            cx, cy = ec
            r = EYE_RADIUS * 0.85
            d.line(
                [(sc(cx - r), sc(cy - r)), (sc(cx + r), sc(cy + r))],
                fill=(0, 0, 0, 255), width=sc(16),
            )
            d.line(
                [(sc(cx - r), sc(cy + r)), (sc(cx + r), sc(cy - r))],
                fill=(0, 0, 0, 255), width=sc(16),
            )
        # 嘴变倒八字(下弧)
        cx, cy = MOUTH_CENTER
        # 先把笑弧盖回
        d.rectangle(
            (sc(cx - MOUTH_RX - 4), sc(cy - MOUTH_RY - 4),
             sc(cx + MOUTH_RX + 4), sc(cy + 2)),
            fill=(0, 0, 0, 255),
        )
        # 挖出倒弧(下弧)— 椭圆下半部分
        d.ellipse(
            (sc(cx - MOUTH_RX), sc(cy - 2),
             sc(cx + MOUTH_RX), sc(cy + MOUTH_RY + 16)),
            fill=(0, 0, 0, 0),
        )
        # 盖住上半(露出下半弧)
        d.rectangle(
            (sc(cx - MOUTH_RX - 4), sc(cy - 2),
             sc(cx + MOUTH_RX + 4), sc(cy + MOUTH_RY)),
            fill=(0, 0, 0, 255),
        )
    elif state == "unknown":
        # 眼睛变成"o.o"(两只空心圆)— 问号脸
        for ec in (EYE_LEFT_CENTER, EYE_RIGHT_CENTER):
            cx, cy = ec
            # 删掉实心眼
            d.ellipse(
                (sc(cx - EYE_RADIUS), sc(cy - EYE_RADIUS),
                 sc(cx + EYE_RADIUS), sc(cy + EYE_RADIUS)),
                fill=(0, 0, 0, 0),
            )
            # 画一个小空心圆
            d.ellipse(
                (sc(cx - EYE_RADIUS * 0.6), sc(cy - EYE_RADIUS * 0.6),
                 sc(cx + EYE_RADIUS * 0.6), sc(cy + EYE_RADIUS * 0.6)),
                outline=(0, 0, 0, 255),
                width=sc(14),
            )

    return img


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


STATES = ("healthy", "partial", "down", "unknown")
MENU_BAR_SIZES = [16, 32, 64, 128, 256, 512, 1024]
APP_ICON_SIZES = [16, 32, 64, 128, 256, 512, 1024]

# 状态颜色(macOS system colors, light/dark 都用同一组,看着都舒服)
#   healthy  → systemGreen
#   partial  → systemYellow
#   down     → systemRed
#   unknown  → systemGray
STATE_COLORS = {
    "healthy":  ( 52, 199,  89),   # systemGreen
    "partial":  (255, 204,   0),   # systemYellow
    "down":     (255,  59,  48),   # systemRed
    "unknown":  (142, 142, 147),   # systemGray
}


def tint_image(img: Image.Image, rgb: tuple[int, int, int]) -> Image.Image:
    """把单色 template 图染成指定颜色(保留 alpha)。

    黑色前景 → rgb; 半透明灰(腮红) → rgb 降亮版;透明背景保留。
    """
    r0, g0, b0 = rgb
    out = img.copy()
    px = out.load()
    w, h = out.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            # 黑色前景 (几乎纯黑) → 改成目标色,保持 alpha
            if r < 40 and g < 40 and b < 40:
                px[x, y] = (r0, g0, b0, a)
            # 灰色腮红 (r=g=b 中间值) → 目标色 + 提亮 30%(让它有层次)
            elif 60 <= r < 150 and r == g == b:
                # 提亮 30% 混合,作为腮红细节
                factor = 0.45
                pr = min(255, int(r0 * factor + 255 * (1 - factor)))
                pg = min(255, int(g0 * factor + 255 * (1 - factor)))
                pb = min(255, int(b0 * factor + 255 * (1 - factor)))
                px[x, y] = (pr, pg, pb, a)
    return out


def main():
    repo = Path(__file__).resolve().parent.parent
    menu_dir = repo / "menubar-agent" / "Resources" / "Assets" / "MenuBar"
    appicon_dir = repo / "menubar-agent" / "Resources" / "Assets" / "AppIcon"
    menu_dir.mkdir(parents=True, exist_ok=True)
    appicon_dir.mkdir(parents=True, exist_ok=True)

    # 清旧文件
    for d in (menu_dir, appicon_dir):
        for p in d.iterdir():
            if p.is_file():
                p.unlink()

    masters: dict[str, Image.Image] = {}
    masters_colored: dict[str, Image.Image] = {}
    for state in STATES:
        img = draw_bot_base(MASTER)
        img = add_expression(img, state)
        masters[state] = img
        # 彩色版本(直接给 menu bar 用,Swift 加载原图)
        colored = tint_image(img, STATE_COLORS[state])
        masters_colored[state] = colored
        colored.save(menu_dir / f"icon-state-{state}@{3}x.png")

    for state in STATES:
        for size in MENU_BAR_SIZES:
            if size == MASTER:
                continue
            # 命名约定:icon-state-{state}@{1x,2x,3x}.png
            # 1024 = @3x (retina 大屏)
            # 512  = @2x (retina 中屏)
            # 256  = @1x 实际是 @2x 的内容但单独尺寸 — 给 Preferences 等其他用途
            if size == 512:
                out_name = f"icon-state-{state}@2x.png"
            elif size == 256:
                out_name = f"icon-state-{state}@1x.png"
            elif size == 128:
                out_name = f"icon-state-{state}-128.png"
            elif size == 64:
                out_name = f"icon-state-{state}-64.png"
            elif size == 32:
                out_name = f"icon-state-{state}-32.png"
            elif size == 16:
                out_name = f"icon-state-{state}-16.png"
            else:
                out_name = f"icon-state-{state}-{size}.png"
            resized = masters_colored[state].resize(
                (size, size), Image.Resampling.LANCZOS,
            )
            resized.save(menu_dir / out_name)

    # App icon — 用彩色 healthy 状态
    healthy = masters_colored["healthy"]
    for size in APP_ICON_SIZES:
        out = healthy.resize((size, size), Image.Resampling.LANCZOS)
        out.save(appicon_dir / f"icon_{size}.png")

    print(f"[draw_bot] menu bar: {menu_dir}")
    for p in sorted(menu_dir.iterdir()):
        print(f"  - {p.name}")
    print(f"[draw_bot] app icon: {appicon_dir}")
    for p in sorted(appicon_dir.iterdir()):
        print(f"  - {p.name}")


if __name__ == "__main__":
    main()

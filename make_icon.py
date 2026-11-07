#!/usr/bin/env python3
"""生成 macOS 应用图标（喜马拉雅上传工具）。

用 Pillow 画一张橙色声波 + 上传箭头的图标，输出标准 .icns。
不依赖任何外部素材，纯本地绘制。

运行：python make_icon.py
"""

from __future__ import annotations

import subprocess
import shutil
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

BASE = Path(__file__).resolve().parent
ICONSET = BASE / "_work" / "icon.iconset"

# 喜马拉雅橙
ORANGE = (255, 106, 22)
ORANGE_DEEP = (232, 88, 12)
CREAM = (255, 247, 240)
WHITE = (255, 255, 255)

SIZES = [
    (16, "icon_16x16.png"),
    (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"),
    (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"),
    (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"),
    (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"),
    (1024, "icon_512x512@2x.png"),
]


def draw_icon(size: int) -> Image.Image:
    """绘制单尺寸图标。"""
    S = size
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角方形底（macOS 风格 squircle 近似）
    pad = int(S * 0.06)
    radius = int(S * 0.22)
    d.rounded_rectangle(
        [pad, pad, S - pad, S - pad],
        radius=radius,
        fill=ORANGE + (255,),
    )
    # 底部渐深，做出立体感
    grad = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for i in range(S):
        a = int(90 * max(0.0, (i / S - 0.55)) / 0.45)
        gd.line([(0, i), (S, i)], fill=ORANGE_DEEP + (a,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [pad, pad, S - pad, S - pad], radius=radius, fill=255
    )
    img.paste(grad, (0, 0), Image.composite(grad.split()[3], Image.new("L", (S, S), 0), mask))

    # 声波：中间高两边低的竖条
    cy = int(S * 0.36)
    bar_w = max(2, int(S * 0.048))
    gap = max(1, int(S * 0.034))
    heights = [0.11, 0.19, 0.27, 0.33, 0.27, 0.19, 0.11]
    total_w = len(heights) * bar_w + (len(heights) - 1) * gap
    x = (S - total_w) // 2
    for h in heights:
        bh = int(S * h)
        d.rounded_rectangle(
            [x, cy - bh // 2, x + bar_w, cy + bh // 2],
            radius=bar_w // 2,
            fill=WHITE + (255,),
        )
        x += bar_w + gap

    # 上传箭头（下方，与声波留出间距）
    ax = S // 2
    top = int(S * 0.62)
    shaft_w = max(2, int(S * 0.052))
    head_w = int(S * 0.14)
    head_h = int(S * 0.115)
    d.rectangle(
        [ax - shaft_w // 2, top, ax + shaft_w // 2, top + int(S * 0.105)],
        fill=CREAM + (255,),
    )
    d.polygon(
        [
            (ax - head_w // 2, top + int(S * 0.045)),
            (ax + head_w // 2, top + int(S * 0.045)),
            (ax, top + head_h),
        ],
        fill=CREAM + (255,),
    )
    # 底部托盘
    tray_y = int(S * 0.80)
    d.rounded_rectangle(
        [ax - int(S * 0.16), tray_y, ax + int(S * 0.16), tray_y + int(S * 0.042)],
        radius=int(S * 0.021),
        fill=CREAM + (255,),
    )

    return img


def main() -> None:
    if ICONSET.exists():
        shutil.rmtree(ICONSET)
    ICONSET.mkdir(parents=True, exist_ok=True)

    master = draw_icon(1024)
    for px, name in SIZES:
        master.resize((px, px), Image.LANCZOS).save(ICONSET / name)
    print(f"▸ 已生成 iconset（{len(SIZES)} 个尺寸）")

    icns = BASE / "icon.icns"
    if icns.exists():
        icns.unlink()
    subprocess.run(
        ["iconutil", "-c", "icns", str(ICONSET), "-o", str(icns)], check=True
    )
    print(f"✅ 图标已生成：{icns}  ({icns.stat().st_size / 1024:.0f} KB)")

    # 预览 PNG（方便直接看效果）
    prev = BASE / "_work" / "icon_preview.png"
    master.resize((256, 256), Image.LANCZOS).save(prev)
    print(f"   预览：{prev}")


if __name__ == "__main__":
    main()

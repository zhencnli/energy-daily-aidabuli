#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Energy Daily - 播客封面生成器 (基于品牌 logo)

把 assets/logo.jpg 规整为符合 Apple Podcasts / 小宇宙 规范的方形封面:
  - 正方形 (自动居中裁切)
  - 边长落在 1400-3000 px 区间 (Apple 硬性要求)
  - RGB / JPEG, 质量 92

源图本身已是成品方形插画, 因此脚本只做「规整」不做重绘:
源边长已在区间内时按原分辨率输出, 避免任何重采样导致的模糊。

用法:
  python make_cover.py
  python make_cover.py --source assets/logo.jpg --out assets/cover.jpg
输出: 一行 JSON (ok / source / out / size / bytes)
"""
import argparse
import json
import os
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SRC = os.path.join(ROOT, "assets", "logo.jpg")
DEFAULT_OUT = os.path.join(ROOT, "assets", "cover.jpg")

MIN_SIDE = 1400
MAX_SIDE = 3000
QUALITY = 92


def to_square(im: Image.Image) -> Image.Image:
    """居中裁切为正方形."""
    w, h = im.size
    if w == h:
        return im
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return im.crop((left, top, left + side, top + side))


def normalize(im: Image.Image):
    """缩放到 1400-3000 区间内, 返回 (图像, 是否重采样)."""
    im = to_square(im)
    side = im.size[0]
    if side > MAX_SIDE:
        return im.resize((MAX_SIDE, MAX_SIDE), Image.LANCZOS), True
    if side < MIN_SIDE:
        return im.resize((MIN_SIDE, MIN_SIDE), Image.LANCZOS), True
    return im, False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=DEFAULT_SRC, help="品牌 logo 源图")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出封面路径")
    args = ap.parse_args()

    if not os.path.exists(args.source):
        print(json.dumps({"ok": False, "error": f"源图不存在: {args.source}"}, ensure_ascii=False))
        return 2

    src = Image.open(args.source)
    im, resampled = normalize(src.convert("RGB"))

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    im.save(args.out, "JPEG", quality=QUALITY, optimize=True, progressive=True)

    print(json.dumps({
        "ok": True,
        "source": os.path.abspath(args.source),
        "source_size": list(src.size),
        "out": os.path.abspath(args.out),
        "size": list(im.size),
        "square": im.size[0] == im.size[1],
        "within_apple_spec": MIN_SIDE <= im.size[0] <= MAX_SIDE,
        "resampled": resampled,
        "quality": QUALITY,
        "bytes": os.path.getsize(args.out),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

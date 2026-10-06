#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Energy Daily - 文字转语音 (edge-tts -> WAV/MP3) + 封面嵌入

依赖: edge-tts, soundfile, mutagen  (已安装在 ~/.workbuddy/binaries/python/envs/default)

用法:
  python tts_edge.py --text-file script.txt --out-wav out.wav [--out-mp3 out.mp3]

可选参数:
  --voice   默认 zh-CN-XiaoxiaoNeural
  --rate    默认 -6%    (语速, 负值必须用 = 号传参: --rate=-6%)
  --pitch   默认 +0Hz
  --volume  默认 +0%
  --cover   封面图, 默认 <ROOT>/assets/cover.jpg; 传 none 可跳过嵌入
  --title / --artist / --album   写入音频元数据(播放器显示);
             不传时自动推导: title 取脚本文件名中的日期, artist/album 取 config.json
  --quiet   静默模式, 只输出一行 JSON 结果

封面会同时嵌入 MP3 的 ID3v2.3 APIC 帧与 WAV 的 ID3 chunk,
使本地播放器、微信/网易云/播客客户端都能显示封面。

输出: 一行 JSON 到 stdout, 形如
  {"ok": true, "wav": "...", "mp3": "...", "duration_sec": 312.4, "chars": 1350,
   "chars_per_min": 259.4, "cover_embedded": {"mp3": true, "wav": true}}
"""
import argparse
import asyncio
import json
import os
import re
import sys

import edge_tts
import soundfile as sf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_COVER = os.path.join(ROOT, "assets", "cover.jpg")


def clean_text(raw: str) -> str:
    """去掉 markdown 标记 / 链接 / 多余空白, 保证纯口播文本."""
    t = raw
    # 去掉 markdown 链接, 保留文字
    t = re.sub(r"\[([^\]]*)\]\((?:[^)]*)\)", r"\1", t)
    # 去掉裸 URL
    t = re.sub(r"https?://\S+", "", t)
    # 去掉 markdown 强调与标题符号
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", t)
    t = re.sub(r"(\*\*|__|\*|`{1,3})", "", t)
    # 去掉列表符号
    t = re.sub(r"(?m)^\s{0,3}[-*+•]\s+", "", t)
    t = re.sub(r"(?m)^\s{0,3}\d+[.、)]\s+", "", t)
    # 去零宽字符
    t = t.replace("\u200b", "").replace("\ufeff", "")
    # 压缩空白
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def count_chars(text: str) -> int:
    """统计播报字数: 去掉所有空白与标点后的可见字符数."""
    return len(re.sub(r"[\s\u3000,、。;；:：!！?？\"'“”‘’()（）\[\]【】—\-…·\.]+", "", text))


async def synth(text: str, voice: str, rate: str, pitch: str, volume: str, mp3_path: str) -> None:
    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume)
    await communicate.save(mp3_path)


def mp3_to_wav(mp3_path: str, wav_path: str):
    data, samplerate = sf.read(mp3_path, always_2d=False)
    sf.write(wav_path, data, samplerate, subtype="PCM_16")
    dur = len(data) / float(samplerate)
    return dur, samplerate


def derive_meta(text_file: str, cfg=None):
    """未显式传入时, 从脚本文件名(含日期)推导标题/主播/专辑默认值."""
    base = os.path.basename(text_file or "")
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", base)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        title = f"Energy Daily · {y}年{mo}月{d}日｜今日新能源头条"
    else:
        title = "Energy Daily｜今日新能源头条"
    artist = "小爱 · Energy Daily"
    album = "Energy Daily | 新能源每日播报"
    if isinstance(cfg, dict) and isinstance(cfg.get("podcast"), dict):
        p = cfg["podcast"]
        artist = p.get("author") or artist
        album = p.get("title") or album
    return title, artist, album


def load_cfg():
    p = os.path.join(ROOT, "config.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None
    return None


def _cover_bytes(cover_path: str):
    """读取封面图并返回 (bytes, mime)."""
    with open(cover_path, "rb") as fh:
        raw = fh.read()
    ext = os.path.splitext(cover_path)[1].lower()
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(ext, "image/jpeg")
    return raw, mime


def embed_mp3_cover(mp3_path: str, cover_path: str, title=None, artist=None, album=None) -> bool:
    """向 MP3 写入 ID3v2.3 标签 (含 APIC 封面帧). 失败返回 False, 不阻断主流程."""
    try:
        from mutagen.id3 import ID3, ID3NoHeaderError, APIC, TIT2, TPE1, TALB
    except Exception:
        return False
    try:
        try:
            tags = ID3(mp3_path)
        except ID3NoHeaderError:
            tags = ID3()
        raw, mime = _cover_bytes(cover_path)
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=raw))
        if title:
            tags.delall("TIT2")
            tags.add(TIT2(encoding=3, text=title))
        if artist:
            tags.delall("TPE1")
            tags.add(TPE1(encoding=3, text=artist))
        if album:
            tags.delall("TALB")
            tags.add(TALB(encoding=3, text=album))
        # v2.3 兼容性最好 (Windows 资源管理器 / iTunes / 主流播放器)
        tags.save(mp3_path, v2_version=3)
        return True
    except Exception:
        return False


def embed_wav_cover(wav_path: str, cover_path: str, title=None, artist=None, album=None) -> bool:
    """向 WAV 写入 ID3 chunk (含 APIC 封面帧). 失败返回 False, 不阻断主流程."""
    try:
        from mutagen.wave import WAVE
        from mutagen.id3 import APIC, TIT2, TPE1, TALB
    except Exception:
        return False
    try:
        audio = WAVE(wav_path)
        if audio.tags is None:
            audio.add_tags()
        raw, mime = _cover_bytes(cover_path)
        audio.tags.delall("APIC")
        audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=raw))
        if title:
            audio.tags.delall("TIT2")
            audio.tags.add(TIT2(encoding=3, text=title))
        if artist:
            audio.tags.delall("TPE1")
            audio.tags.add(TPE1(encoding=3, text=artist))
        if album:
            audio.tags.delall("TALB")
            audio.tags.add(TALB(encoding=3, text=album))
        audio.save()
        return True
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text-file", required=True)
    ap.add_argument("--out-wav", required=True)
    ap.add_argument("--out-mp3", default=None)
    ap.add_argument("--voice", default=DEFAULT_VOICE)
    ap.add_argument("--rate", default="-6%",
                    help="语速; 负值必须用 = 号传参, 如 --rate=-6%% (否则被 argparse 当作选项)")
    ap.add_argument("--pitch", default="+0Hz")
    ap.add_argument("--volume", default="+0%")
    ap.add_argument("--cover", default=DEFAULT_COVER,
                    help="封面图路径; 传 none 跳过嵌入")
    ap.add_argument("--title", default=None, help="默认从脚本文件名日期推导")
    ap.add_argument("--artist", default=None, help="默认取 config.json 的 podcast.author")
    ap.add_argument("--album", default=None, help="默认取 config.json 的 podcast.title")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    cfg = load_cfg()
    d_title, d_artist, d_album = derive_meta(args.text_file, cfg)
    title = args.title or d_title
    artist = args.artist or d_artist
    album = args.album or d_album

    with open(args.text_file, "r", encoding="utf-8") as fh:
        raw = fh.read()
    text = clean_text(raw)
    if not text:
        print(json.dumps({"ok": False, "error": "empty text"}, ensure_ascii=False))
        return 2

    mp3_path = args.out_mp3
    tmp_mp3 = False
    if not mp3_path:
        mp3_path = os.path.splitext(args.out_wav)[0] + ".__tmp__.mp3"
        tmp_mp3 = True

    for p in (args.out_wav, mp3_path):
        os.makedirs(os.path.dirname(os.path.abspath(p)) or ".", exist_ok=True)

    try:
        asyncio.run(synth(text, args.voice, args.rate, args.pitch, args.volume, mp3_path))
        dur, sr = mp3_to_wav(mp3_path, args.out_wav)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1

    # 封面嵌入 (在 wav 生成之后, 避免被 RIFF 重写覆盖)
    cover = None if str(args.cover).lower() in ("none", "no", "") else args.cover
    embedded = {"mp3": False, "wav": False, "cover": None}
    if cover and os.path.exists(cover):
        embedded["cover"] = os.path.abspath(cover)
        embedded["mp3"] = embed_mp3_cover(mp3_path, cover, title, artist, album)
        embedded["wav"] = embed_wav_cover(args.out_wav, cover, title, artist, album)

    # 嵌图会改变 wav 体积但不改变时长; 重新读取确保时长准确
    try:
        info = sf.info(args.out_wav)
        dur = info.frames / float(info.samplerate)
    except Exception:
        pass

    if tmp_mp3 and os.path.exists(mp3_path):
        os.remove(mp3_path)

    chars = count_chars(text)
    cpm = round(chars / (dur / 60.0), 1) if dur > 0 else 0.0
    result = {
        "ok": True,
        "voice": args.voice,
        "wav": os.path.abspath(args.out_wav),
        "mp3": os.path.abspath(args.out_mp3) if args.out_mp3 else None,
        "duration_sec": round(dur, 1),
        "duration_hms": f"{int(dur // 60):02d}:{int(dur % 60):02d}",
        "sample_rate": sr,
        "chars": chars,
        "chars_per_min": cpm,
        "tags": {"title": title, "artist": artist, "album": album},
        "cover_embedded": embedded,
        "wav_bytes": os.path.getsize(args.out_wav),
        "mp3_bytes": os.path.getsize(args.out_mp3) if args.out_mp3 else None,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

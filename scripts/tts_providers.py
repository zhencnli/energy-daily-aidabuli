#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Energy Daily · 语音生成 provider 抽象层

支持:
  - EdgeTTSProvider   : 微软 edge-tts（免费，开箱即用，可作为测试/兜底）
  - AliyunTTSProvider : 阿里通义 DashScope CosyVoice（需 DASHSCOPE_API_KEY）

功能:
  - 解析稿件：男声独讲 / 男女对话（按 老锂: / 小爱: 等标记切分说话人）
  - 分句合成（避免单次请求过长）
  - 多说话人多段音频合并为单一文件（纯 Python：PCM 拼接；单人时直接保留 mp3）
  - 封面嵌入（MP3/WAV 的 ID3 APIC）

用法（被 studio.py 调用）:
  from tts_providers import synthesize
  res = synthesize(text, mode="dialogue", provider_name="aliyun",
                  out_path="studio_output/xxx.mp3", cfg=cfg, cover="assets/cover.jpg")
"""
import base64
import io
import json
import os
import re
import shutil
import sys
import tempfile

import numpy as np
import soundfile as sf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_cfg():
    p = os.path.join(ROOT, "config.json")
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def clean_text(raw: str) -> str:
    """去掉 markdown / 链接 / 多余空白，保留纯口播文本。复用 tts_edge 的逻辑。"""
    try:
        from tts_edge import clean_text as _ct
        return _ct(raw)
    except Exception:
        t = re.sub(r"https?://\S+", "", raw or "")
        t = re.sub(r"(\*\*|__|\*|`{1,3})", "", t)
        t = re.sub(r"[ \t]+", " ", t)
        return t.strip()


# ----------------------------------------------------------------- 稿件解析
_SPLIT = re.compile(r"(?<=[。！？!?；;．\n])")
_ROLE_MALE = re.compile(r"^(老锂|男声?|L)\s*[:：]\s*(.*)$")
_ROLE_FEMALE = re.compile(r"^(小爱|女声?|A)\s*[:：]\s*(.*)$")


def chunk_sentences(text, max_chars=200):
    parts = [p.strip() for p in _SPLIT.split(text) if p.strip()]
    out, buf = [], ""
    for p in parts:
        if len(buf) + len(p) <= max_chars:
            buf += p
        else:
            if buf:
                out.append(buf)
            buf = p
    if buf:
        out.append(buf)
    return out


def parse_script(text, mode, default_role="male"):
    """返回 [{'text':..., 'role':'male'|'female'}, ...]。
    mode='solo'    : 全文为单一男声（老锂）。
    mode='dialogue': 按 老锂:/小爱: 等标记切分；无标记行沿用上一句说话人。
    """
    text = clean_text(text)
    if mode == "solo":
        segs = []
        for s in chunk_sentences(text):
            segs.append({"text": s, "role": "male"})
        return segs

    segs, cur = [], default_role
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _ROLE_MALE.match(line)
        if m:
            cur, body = "male", m.group(2).strip()
        else:
            m = _ROLE_FEMALE.match(line)
            if m:
                cur, body = "female", m.group(2).strip()
            else:
                body = line  # 无标记，沿用上一句说话人
        if not body:
            continue
        for s in chunk_sentences(body):
            segs.append({"text": s, "role": cur})
    if not segs:  # 兜底：整段当作男声独讲
        for s in chunk_sentences(text):
            segs.append({"text": s, "role": "male"})
    return segs


# ----------------------------------------------------------------- provider
class TTSProvider:
    def __init__(self, cfg):
        self.cfg = cfg or {}

    def synth_segment(self, text, role, out_mp3, **kw):
        raise NotImplementedError

    def role_voice(self, role):
        raise NotImplementedError

    def merge(self, temp_paths, out_path):
        """把多段临时音频合并为单一文件。单人且输出 mp3 时直接复制；
        否则解码为 PCM 拼接并写出 WAV（纯 Python，无 ffmpeg 依赖）。"""
        if len(temp_paths) == 1 and out_path.lower().endswith(".mp3"):
            shutil.copy(temp_paths[0], out_path)
            return out_path
        arrays, sr = [], None
        for tp in temp_paths:
            data, sri = sf.read(tp, always_2d=False)
            arrays.append(data)
            sr = sri
        big = np.concatenate(arrays) if arrays else np.zeros(1, dtype=np.float32)
        final = out_path
        if not final.lower().endswith(".wav"):
            final = os.path.splitext(out_path)[0] + ".wav"
        sf.write(final, big, sr, subtype="PCM_16")
        return final


class EdgeTTSProvider(TTSProvider):
    def __init__(self, cfg):
        super().__init__(cfg)
        e = self.cfg.get("tts", {}).get("edge", {})
        self.voice_male = e.get("voice_male", "zh-CN-YunxiNeural")
        self.voice_female = e.get("voice_female", "zh-CN-XiaoxiaoNeural")
        self.rate = e.get("rate", "-6%")
        self.pitch = e.get("pitch", "+0Hz")
        self.volume = e.get("volume", "+0%")

    def role_voice(self, role):
        return self.voice_male if role == "male" else self.voice_female

    def synth_segment(self, text, role, out_mp3, **kw):
        import asyncio
        import edge_tts
        voice = self.role_voice(role)
        asyncio.run(edge_tts.Communicate(text, voice, rate=self.rate,
                                         pitch=self.pitch, volume=self.volume).save(out_mp3))


class AliyunTTSProvider(TTSProvider):
    def __init__(self, cfg):
        super().__init__(cfg)
        a = self.cfg.get("tts", {}).get("aliyun", {})
        self.api_key = os.environ.get(a.get("api_key_env", "DASHSCOPE_API_KEY"), "")
        self.endpoint = a.get("endpoint", "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation")
        self.model = a.get("model", "cosyvoice-v1")
        self.voice_male = a.get("voice_male", "longhua")
        self.voice_female = a.get("voice_female", "longxiaochun")
        self.fmt = a.get("format", "mp3")
        self.rate = a.get("rate", 1.0)

    def role_voice(self, role):
        return self.voice_male if role == "male" else self.voice_female

    def synth_segment(self, text, role, out_mp3, **kw):
        import requests
        if not self.api_key:
            raise RuntimeError("缺少环境变量 %s（请填入阿里通义 DashScope API Key）"
                               % self.cfg.get("tts", {}).get("aliyun", {}).get("api_key_env"))
        voice = self.role_voice(role)
        payload = {
            "model": self.model,
            "input": {"text": text, "voice": voice},
            "parameters": {"format": self.fmt, "rate": self.rate, "volume": 100},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # 长文本走异步任务：提交 -> 轮询（DashScope 标准模式）
        r = requests.post(self.endpoint, json=payload, headers=headers, timeout=120)
        r.raise_for_status()
        data = r.json()
        if data.get("code") is not None and data.get("code") != 200:
            raise RuntimeError(f"阿里 TTS 错误 {data.get('code')}: {data.get('message')}")
        out = (data.get("output") or {}).get("results", [{}])[0]
        audio = out.get("audio") or {}
        if audio.get("data"):
            raw = base64.b64decode(audio["data"])
        elif audio.get("url"):
            raw = requests.get(audio["url"], timeout=120).content
        else:
            raise RuntimeError("阿里 TTS 返回中无 audio.data / url")
        with open(out_mp3, "wb") as fh:
            fh.write(raw)


def get_provider(name, cfg):
    if name == "edge":
        return EdgeTTSProvider(cfg)
    if name == "aliyun":
        return AliyunTTSProvider(cfg)
    raise ValueError(f"未知 TTS provider: {name}")


def embed_cover(final_path, cover_path, title=None, artist=None, album=None):
    try:
        from tts_edge import embed_mp3_cover, embed_wav_cover
        if final_path.lower().endswith(".mp3"):
            return embed_mp3_cover(final_path, cover_path, title, artist, album)
        if final_path.lower().endswith(".wav"):
            return embed_wav_cover(final_path, cover_path, title, artist, album)
    except Exception:
        return False
    return False


def synthesize(text, mode, provider_name, out_path, cfg=None, cover=None,
               title=None, artist=None, album=None, progress=None):
    """生成语音主入口。
    text        : 原始稿件
    mode        : 'solo' | 'dialogue'
    provider_name: 'edge' | 'aliyun'
    out_path    : 输出音频路径（扩展名 .mp3 或 .wav）
    progress    : 可选回调 progress(stage, i, total)
    返回 dict: {ok, out_path, duration_sec, chars, segments, ...}
    """
    cfg = cfg or _load_cfg()
    segs = parse_script(text, mode)
    if not segs:
        return {"ok": False, "error": "稿件为空或无法解析"}
    provider = get_provider(provider_name, cfg)
    total = len(segs)
    tmp_dir = tempfile.mkdtemp(prefix="tts_")
    tmp_paths = []
    for i, seg in enumerate(segs, 1):
        tp = os.path.join(tmp_dir, f"seg_{i:03d}.mp3")
        try:
            provider.synth_segment(seg["text"], seg["role"], tp)
        except Exception as exc:
            return {"ok": False, "error": f"第{i}/{total}段合成失败: {type(exc).__name__}: {exc}"}
        if os.path.exists(tp) and os.path.getsize(tp) > 0:
            tmp_paths.append(tp)
        if progress:
            progress("synth", i, total)
    if not tmp_paths:
        return {"ok": False, "error": "未生成任何音频段"}

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    final = provider.merge(tmp_paths, out_path)

    dur = 0.0
    try:
        info = sf.info(final)
        dur = info.frames / float(info.samplerate)
    except Exception:
        pass

    if cover and os.path.exists(cover):
        embed_cover(final, cover, title, artist, album)

    try:
        shutil.rmtree(tmp_dir)
    except Exception:
        pass

    chars = sum(len(re.sub(r"[\s\u3000]+", "", s["text"])) for s in segs)
    return {
        "ok": True,
        "out_path": os.path.abspath(final),
        "duration_sec": round(dur, 1),
        "duration_hms": f"{int(dur // 60):02d}:{int(dur % 60):02d}",
        "chars": chars,
        "segments": total,
    }


if __name__ == "__main__":
    # 简单自测（edge-tts，单人）
    import sys as _sys
    demo = "这是一段测试语音。新能源是未来十年最确定的产业方向之一。"
    r = synthesize(demo, "solo", "edge", "studio_output/_selftest.mp3", cover="assets/cover.jpg")
    print(json.dumps(r, ensure_ascii=False))
    _sys.exit(0 if r.get("ok") else 1)

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Energy Daily · 语音生成 provider 抽象层

支持:
  - EdgeTTSProvider   : 微软 edge-tts（免费，开箱即用，可作为测试/兜底）
  - AliyunTTSProvider : 阿里通义 DashScope CosyVoice（需 DASHSCOPE_API_KEY）
  - TencentLongFormTTSProvider : 腾讯云长文本语音合成（智宇501003/智菊501002，零第三方依赖）

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
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.request

import numpy as np
import soundfile as sf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_dotenv_into_environ(path=None):
    """把仓库根 .env 的键值注入 os.environ（不覆盖已存在的环境变量）。

    provider（腾讯 / 阿里）直接读 os.environ 取密钥，而 publish.load_env() 只把
    .env 读成字典（供 node 子进程用），从不写进 os.environ。故此处统一补上，
    让 GUI 与命令行两种入口都能读到；已显式导出的环境变量优先级更高、不被覆盖。
    """
    p = path or os.path.join(ROOT, ".env")
    if not os.path.exists(p):
        return {}
    loaded = {}
    try:
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if not k:
                continue
            loaded[k] = v
            if k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass
    return loaded


# 模块导入即注入 .env：保证 studio GUI 与命令行都无需手动 export
load_dotenv_into_environ()


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


class TencentLongFormTTSProvider(TTSProvider):
    """腾讯云长文本语音合成（异步，零第三方依赖，仅标准库）。

    流程: CreateTtsTask -> DescribeTtsTaskStatus(轮询) -> 下载 ResultUrl。
    智宇 = 501003（阅读男声）—— 正对 Insight 老锂的沉稳讲书场景。
    智菊 = 501002（阅读女声）—— 用于男女对话里的小爱。

    凭证: 与 COS 同一对 SecretId/SecretKey（账号级通用），但签名走 TC3-HMAC-SHA256，
          与 COS 的 q-sign 完全不同，不能复用 COS SDK 的签名。
    """

    HOST = "tts.tencentcloudapi.com"
    VERSION = "2019-08-23"
    SERVICE = "tts"

    def __init__(self, cfg):
        super().__init__(cfg)
        t = self.cfg.get("tts", {}).get("tencent", {})
        # 默认复用 COS 子账号密钥（同一账号通用）；若单独开 TTS 子账号可改 env 名
        self.secret_id = os.environ.get(t.get("secret_id_env", "TENCENT_COS_SECRET_ID"), "")
        self.secret_key = os.environ.get(t.get("secret_key_env", "TENCENT_COS_SECRET_KEY"), "")
        self.voice_male = int(t.get("voice_male", 501003))    # 智宇
        self.voice_female = int(t.get("voice_female", 501002))  # 智菊
        self.codec = t.get("codec", "mp3")
        self.sample_rate = int(t.get("sample_rate", 16000))
        self.speed = int(t.get("speed", 0))
        self.volume = int(t.get("volume", 0))
        self.host = t.get("host", self.HOST)
        if not self.secret_id or not self.secret_key:
            raise RuntimeError(
                "缺少腾讯云凭证：请在 .env 设置 TENCENT_COS_SECRET_ID / TENCENT_COS_SECRET_KEY"
                "（与 COS 同一账号即可），并为该子账号添加 QcloudTTSFullAccess 权限。")

    def role_voice(self, role):
        return self.voice_male if role == "male" else self.voice_female

    # ---- TC3-HMAC-SHA256 签名（仅标准库） ----
    def _sign(self, payload, action):
        now = int(time.time())
        date = time.strftime("%Y-%m-%d", time.gmtime(now))
        hashed = hashlib.sha256(payload).hexdigest()
        canonical = (
            "POST\n/\n\n"
            "content-type:application/json; charset=utf-8\n"
            f"host:{self.host}\n"
            f"x-tc-action:{action.lower()}\n\n"
            "content-type;host;x-tc-action\n" + hashed
        )
        cred_scope = f"{date}/{self.SERVICE}/tc3_request"
        sts = (f"TC3-HMAC-SHA256\n{now}\n{cred_scope}\n"
               + hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        secret_date = hmac.new(b"TC3" + self.secret_key.encode("utf-8"),
                               date.encode("utf-8"), hashlib.sha256).digest()
        secret_service = hmac.new(secret_date, self.SERVICE.encode("utf-8"),
                                  hashlib.sha256).digest()
        secret_signing = hmac.new(secret_service, b"tc3_request", hashlib.sha256).digest()
        sig = hmac.new(secret_signing, sts.encode("utf-8"), hashlib.sha256).hexdigest()
        auth = (f"TC3-HMAC-SHA256 Credential={self.secret_id}/{cred_scope}, "
                f"SignedHeaders=content-type;host;x-tc-action, Signature={sig}")
        return {
            "Authorization": auth,
            "Content-Type": "application/json; charset=utf-8",
            "Host": self.host,
            "X-TC-Action": action,
            "X-TC-Version": self.VERSION,
            "X-TC-Timestamp": str(now),
        }

    def _post(self, action, body):
        payload = json.dumps(body).encode("utf-8")
        headers = self._sign(payload, action)
        req = urllib.request.Request(
            f"https://{self.host}/", data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"腾讯云 API {action} HTTP {e.code}: "
                + e.read().decode("utf-8", "ignore"))

    def _create_task(self, text, voice_type):
        resp = self._post("CreateTtsTask", {
            "Text": text,
            "VoiceType": voice_type,
            "Codec": self.codec,
            "SampleRate": self.sample_rate,
            "Speed": self.speed,
            "Volume": self.volume,
            "PrimaryLanguage": 1,            # 中文
            "EnableSubtitle": False,
            "EmotionCategory": "neutral",
            "EmotionIntensity": 100,
        })
        resp = resp.get("Response") or {}
        err = resp.get("Error")
        if err:
            code = err.get("Code", "")
            msg = err.get("Message", "")
            hint = ""
            if "ServerNotOpen" in code:
                hint = ("\n  → 腾讯云「语音合成」服务尚未开通。请用【主账号】登录 "
                        "https://console.cloud.tencent.com/tts ，完成实名/人脸认证、"
                        "勾选《用户协议》后点「立即开通」。开通后无需更换密钥，直接重试即可。")
            elif "AuthFailure" in code or "UnauthorizedOperation" in code or "NoPermission" in code:
                hint = ("\n  → 鉴权失败：确认该子账号已关联 QcloudTTSFullAccess，"
                        "且 .env 中密钥完整、系统时间/时区正确。")
            elif "LimitExceeded" in code or "Insufficient" in code or "Balance" in code:
                hint = ("\n  → 可能余额不足或触发限频。长文本接口无免费额度，"
                        "请确认账户后付费可用/余额充足。")
            raise RuntimeError(f"CreateTtsTask 错误 {code}: {msg}{hint}")
        return (resp.get("Data") or {}).get("TaskId")

    def _wait_task(self, task_id, timeout=1800):
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(3)
            resp = self._post("DescribeTtsTaskStatus", {"TaskId": task_id})
            d = (resp.get("Response") or {}).get("Data", {})
            status_str = d.get("StatusStr") or ""
            status_code = d.get("Status")
            if status_str == "success" or status_code == 2:
                return d.get("ResultUrl")
            if status_str == "fail" or status_code == 3:
                raise RuntimeError(f"任务 {task_id} 失败：{d.get('ErrorMsg')}")
        raise RuntimeError(f"任务 {task_id} 超时（> {timeout}s）未返回结果")

    def _download(self, url, out_path):
        req = urllib.request.Request(
            url, headers={"User-Agent": "energy-daily-studio/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r, open(out_path, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)

    def synth_block(self, text, role, out_path):
        """把一整段（同一说话人）作为一个长文本任务提交，返回 mp3 路径。"""
        voice = self.role_voice(role)
        task_id = self._create_task(text, voice)
        url = self._wait_task(task_id)
        if not url:
            raise RuntimeError("任务成功但未返回 ResultUrl")
        self._download(url, out_path)
        return out_path

    def synth_blocks(self, segs, tmp_dir, progress=None):
        """覆盖基类：按说话人合并为连续块，每块一次长文本任务，避免逐句碎任务。"""
        blocks, cur = [], None
        for s in segs:
            if cur and cur["role"] == s["role"]:
                cur["text"] += s["text"]
            else:
                cur = {"role": s["role"], "text": s["text"]}
                blocks.append(cur)
        total = len(blocks)
        paths = []
        for i, b in enumerate(blocks, 1):
            tp = os.path.join(tmp_dir, f"blk_{i:03d}.mp3")
            self.synth_block(b["text"], b["role"], tp)
            if os.path.exists(tp) and os.path.getsize(tp) > 0:
                paths.append(tp)
            if progress:
                progress("synth", i, total)
        return paths

    def merge(self, tmp_paths, out_path):
        """单人（或单块）直接复制；多人对话按 ID3 剥离后字节拼接（同源参数一致，可安全拼接）。"""
        if len(tmp_paths) == 1:
            shutil.copy(tmp_paths[0], out_path)
            return out_path
        return self._concat_mp3(tmp_paths, out_path)

    @staticmethod
    def _concat_mp3(paths, out_path):
        with open(out_path, "wb") as out:
            for p in paths:
                data = open(p, "rb").read()
                if data[:3] == b"ID3" and len(data) > 10:   # 去开头 ID3v2
                    size = struct.unpack(">I", data[6:10])[0]
                    data = data[10 + size:]
                if len(data) > 128 and data[-128:][:3] == b"TAG":  # 去结尾 ID3v1
                    data = data[:-128]
                out.write(data)
        return out_path


def get_provider(name, cfg):
    if name == "edge":
        return EdgeTTSProvider(cfg)
    if name == "aliyun":
        return AliyunTTSProvider(cfg)
    if name == "tencent":
        return TencentLongFormTTSProvider(cfg)
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


def _audio_duration(path):
    """优先用 mutagen 取时长（对 mp3 最稳），失败再回退 soundfile。"""
    try:
        from mutagen import File as MF
        m = MF(path)
        if m and getattr(m, "info", None):
            return float(m.info.length)
    except Exception:
        pass
    try:
        info = sf.info(path)
        return info.frames / float(info.samplerate)
    except Exception:
        pass
    return 0.0


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
    total = len(segs)
    provider = get_provider(provider_name, cfg)
    tmp_dir = tempfile.mkdtemp(prefix="tts_")
    tmp_paths = []
    if hasattr(provider, "synth_blocks"):
        # 腾讯长文本等：按说话人合并后整段提交（更省任务、无接缝）
        tmp_paths = provider.synth_blocks(segs, tmp_dir, progress)
    else:
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

    dur = _audio_duration(final)

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
    # 命令行自测 / 单集生成：
    #   python tts_providers.py tencent            # 用内置样例
    #   python tts_providers.py tencent script.txt # 直接合成稿件文件（单人，智宇）
    import sys as _sys
    provider = _sys.argv[1] if len(_sys.argv) > 1 else "edge"
    src = _sys.argv[2] if len(_sys.argv) > 2 else None
    if src and os.path.exists(src):
        with open(src, encoding="utf-8") as fh:
            demo = fh.read()
    else:
        demo = "这是一段测试语音。新能源是未来十年最确定的产业方向之一。"
    out = "studio_output/_selftest.mp3"
    r = synthesize(demo, "solo", provider, out, cover="assets/cover.jpg")
    print(json.dumps(r, ensure_ascii=False))
    _sys.exit(0 if r.get("ok") else 1)

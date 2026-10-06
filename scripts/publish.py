#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Energy Daily - 播客发布器

功能:
  1. 上传当日音频 (mp3 用于播客 enclosure, wav 作为原始交付) 到腾讯云 COS
  2. 维护 data/episodes.json (按期累积, 同日幂等覆盖)
  3. 渲染符合 Apple Podcasts / 小宇宙 规范的 RSS 2.0 feed
  4. 上传 RSS 与封面图到 COS, 输出可填入播客平台的托管地址

用法:
  python publish.py --mp3 audio/energydaily-2026-10-05.mp3 \
                    --wav audio/energydaily-2026-10-05.wav \
                    --title "..." --summary "..." --date 2026-10-05 \
                    --duration 372

  --dry-run   只生成本地 RSS, 不上传
输出: 一行 JSON (ok / feed_url / episode_url / wav_url / count)
"""
import argparse
import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config.json")
EPISODES = os.path.join(ROOT, "data", "episodes.json")
RSS_LOCAL = os.path.join(ROOT, "data", "podcast.xml")
CST = timezone(timedelta(hours=8))


def load_config():
    with open(CONFIG, encoding="utf-8") as fh:
        return json.load(fh)


def load_env():
    env = {}
    p = os.path.join(ROOT, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'\"")
    return env


def cos(cfg, args, dry=False):
    """调用 node 版 COS 上传工具, 返回解析后的 dict."""
    if dry:
        return {"ok": True, "dry_run": True, "key": args[args.index("--key") + 1]}
    env = dict(os.environ)
    env.update(load_env())
    node_bin = cfg.get("node_bin")
    script = os.path.join(ROOT, "scripts", "cos_upload.mjs")
    proc = subprocess.run(
        [node_bin, script] + args,
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=ROOT,
    )
    out = (proc.stdout or "").strip()
    if not out:
        return {"ok": False, "error": f"(no stdout) {proc.stderr.strip()[:300]}"}
    # 取最后一行 JSON (脚本输出可能多行美化)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", out, re.S)
        return json.loads(m.group(0)) if m else {"ok": False, "error": out[:300]}


def load_episodes():
    if os.path.exists(EPISODES):
        with open(EPISODES, encoding="utf-8") as fh:
            return json.load(fh)
    return []


def save_episodes(eps):
    os.makedirs(os.path.dirname(EPISODES), exist_ok=True)
    with open(EPISODES, "w", encoding="utf-8") as fh:
        json.dump(eps, fh, ensure_ascii=False, indent=2)


def esc(s):
    return html.escape(str(s or ""), quote=False)


def norm_explicit(v, default="false"):
    """Apple 要求 <itunes:explicit> 只能是 true / false.

    历史写法 yes/no 会被校验器判为非法
    (W3C Feed Validator: 'itunes:explicit must be "true" or "false"')。
    这里统一归一化, 兼容配置里可能出现的各种写法。
    """
    s = str(v if v is not None else default).strip().lower()
    if s in ("true", "yes", "y", "1", "explicit"):
        return "true"
    if s in ("false", "no", "n", "0", "clean", "none"):
        return "false"
    return default


def cos_origin(env):
    """COS 直连基址 (无 CDN 时的回退)."""
    bucket = env.get("TENCENT_COS_BUCKET", "")
    region = env.get("TENCENT_COS_REGION", "")
    return f"https://{bucket}.cos.{region}.myqcloud.com" if bucket else ""


def public_base(cfg, env):
    """对外基址.

    优先用 config.cos.public_base (CDN / 边缘代理前缀), 未配置则回退到 COS 直连。
    背景: 2024-01-01 后创建的 COS 桶, 默认域名/静态网站域名/全球加速域名会对
    所有对象强制追加 `Content-Disposition: attachment` + `x-cos-force-download: true`,
    且对象级 inline 无法覆盖, 只有自定义域名或 CDN 域名不受影响。
    """
    b = (cfg.get("cos", {}).get("public_base") or "").strip().rstrip("/")
    return b or cos_origin(env)


def rewrite_host(url, origin, target):
    """把已存进 episodes.json 的 COS 直链换成对外基址 (换域名时无需重传音频)."""
    if not url or not origin or not target or origin == target:
        return url
    return target + url[len(origin):] if url.startswith(origin) else url


def render_rss(cfg, episodes, env):
    p = cfg["podcast"]
    base = public_base(cfg, env) or "https://example.com"
    origin = cos_origin(env)
    feed_url = f"{base}/{cfg['cos']['key_rss']}"
    cover_url = f"{base}/{cfg['cos']['key_cover']}"
    site = p.get("website") or feed_url

    by_date = sorted(episodes, key=lambda e: e["date"], reverse=True)
    n = len(by_date)
    items = []
    for idx, e in enumerate(by_date):
        dt = datetime.strptime(e["date"], "%Y-%m-%d").replace(tzinfo=CST)
        ep_no = n - idx  # 最早为第 1 期
        items.append(f"""    <item>
      <title>{esc(e['title'])}</title>
      <description><![CDATA[{e.get('summary', '')}]]></description>
      <itunes:summary><![CDATA[{e.get('summary', '')}]]></itunes:summary>
      <itunes:subtitle>{esc(e.get('subtitle', '新能源每日播报'))}</itunes:subtitle>
      <enclosure url="{esc(rewrite_host(e['url'], origin, base))}" length="{e.get('length', 0)}" type="audio/mpeg"/>
      <guid isPermaLink="false">energydaily-{e['date']}</guid>
      <pubDate>{format_datetime(dt)}</pubDate>
      <itunes:duration>{e.get('duration_hms', '00:00')}</itunes:duration>
      <itunes:episode>{ep_no}</itunes:episode>
      <itunes:episodeType>full</itunes:episodeType>
      <itunes:explicit>{norm_explicit(p.get('explicit'))}</itunes:explicit>
      <link>{esc(site)}</link>
    </item>""")

    sub = (p.get("subtitle") or "").strip()
    _sub = f"    <itunes:subtitle>{esc(sub)}</itunes:subtitle>" if sub else ""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{esc(p['title'])}</title>
    <link>{esc(site)}</link>
    <language>{p.get('language', 'zh-cn')}</language>
    <copyright>{esc(p.get('copyright', ''))}</copyright>
    <description><![CDATA[{p.get('description', '')}]]></description>
    <itunes:author>{esc(p.get('author', ''))}</itunes:author>
{_sub}
    <itunes:summary><![CDATA[{p.get('description', '')}]]></itunes:summary>
    <itunes:owner>
      <itunes:name>{esc(p.get('owner_name', p.get('author', '')))}</itunes:name>
      <itunes:email>{esc(p.get('email', ''))}</itunes:email>
    </itunes:owner>
    <itunes:image href="{esc(cover_url)}"/>
    <image>
      <url>{esc(cover_url)}</url>
      <title>{esc(p['title'])}</title>
      <link>{esc(site)}</link>
    </image>
    <itunes:category text="{esc(p.get('category', 'Business'))}">
      <itunes:category text="{esc(p.get('subcategory', 'Investing'))}"/>
    </itunes:category>
    <itunes:explicit>{norm_explicit(p.get('explicit'))}</itunes:explicit>
    <itunes:type>episodic</itunes:type>
    <itunes:complete>no</itunes:complete>
    <atom:link href="{esc(feed_url)}" rel="self" type="application/rss+xml"/>
{chr(10).join(items)}
  </channel>
</rss>
"""


def main():
    cfg = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--mp3")
    ap.add_argument("--wav", default=None)
    ap.add_argument("--title")
    ap.add_argument("--summary", default="")
    ap.add_argument("--subtitle", default="新能源每日播报")
    ap.add_argument("--date", help="YYYY-MM-DD")
    ap.add_argument("--duration", type=float, default=0.0, help="秒")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rss-only", action="store_true",
                    help="不传音频、不改 episodes.json，仅按现有台账重建并上传 RSS"
                         "（改了 config 里的品牌信息后用它即时生效）")
    args = ap.parse_args()

    # 必须把 os.environ 也带上：云端没有 .env，只有环境变量。
    # 只用 load_env() 会让 cos_origin() 拿到空桶名 → public_base 回退到 example.com，
    # 把错误域名写进线上 RSS 的封面与 feed 自引用链接。
    env = dict(os.environ)
    env.update(load_env())

    if args.rss_only:
        eps = load_episodes()
        xml = render_rss(cfg, eps, env)
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), "podcast-rebuild.xml")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(xml)
        r = cos(cfg, ["upload", "--file", tmp, "--key", cfg["cos"]["key_rss"],
                      "--content-type", "application/rss+xml; charset=utf-8"], args.dry_run)
        if not r.get("ok"):
            print(json.dumps({"ok": False, "stage": "upload-rss", "detail": r}, ensure_ascii=False))
            return 1
        print(json.dumps({
            "ok": True, "rss_only": True,
            "feed_url": f"{public_base(cfg, env)}/{cfg['cos']['key_rss']}",
            "episode_count": len(eps),
        }, ensure_ascii=False, indent=2))
        return 0

    for missing in ("mp3", "title", "date"):
        if not getattr(args, missing):
            ap.error(f"缺少 --{missing}（--rss-only 模式才不需要）")

    c = cfg["cos"]
    base_name = cfg["naming"]["audio_basename"].format(date=args.date)
    key_mp3 = f"{c['prefix_audio']}/{base_name}.mp3"
    key_wav = f"{c['prefix_audio']}/{base_name}.wav"

    # 1) 上传音频
    r_mp3 = cos(cfg, ["upload", "--file", args.mp3, "--key", key_mp3,
                      "--content-type", "audio/mpeg"], args.dry_run)
    if not r_mp3.get("ok"):
        print(json.dumps({"ok": False, "stage": "upload-mp3", "detail": r_mp3}, ensure_ascii=False))
        return 1
    r_wav = {"url": None}
    if args.wav and os.path.exists(args.wav):
        r_wav = cos(cfg, ["upload", "--file", args.wav, "--key", key_wav,
                          "--content-type", "audio/wav"], args.dry_run)

    # 2) 维护 episode 列表 (同日幂等)
    eps = [e for e in load_episodes() if e["date"] != args.date]
    dur_hms = f"{int(args.duration // 60):02d}:{int(args.duration % 60):02d}" if args.duration else "00:00"
    eps.append({
        "date": args.date,
        "title": args.title,
        "subtitle": args.subtitle,
        "summary": args.summary,
        "url": r_mp3.get("url") if not args.dry_run else f"{base_name}.mp3",
        "length": r_mp3.get("size", 0),
        "duration_hms": dur_hms,
        "duration_sec": args.duration,
        "wav_url": r_wav.get("url"),
    })
    eps.sort(key=lambda e: e["date"])
    save_episodes(eps)

    # 3) 渲染 RSS
    xml = render_rss(cfg, eps, env)
    os.makedirs(os.path.dirname(RSS_LOCAL), exist_ok=True)
    with open(RSS_LOCAL, "w", encoding="utf-8") as fh:
        fh.write(xml)

    # 4) 上传 RSS
    r_rss = cos(cfg, ["upload", "--file", RSS_LOCAL, "--key", c["key_rss"],
                      "--content-type", "application/rss+xml; charset=utf-8"], args.dry_run)
    if not r_rss.get("ok"):
        print(json.dumps({"ok": False, "stage": "upload-rss", "detail": r_rss}, ensure_ascii=False))
        return 1

    base = public_base(cfg, env) or "https://example.com"
    origin = cos_origin(env)
    feed_url = rewrite_host(r_rss.get("url"), origin, base) or f"{base}/{c['key_rss']}"
    print(json.dumps({
        "ok": True,
        "date": args.date,
        "feed_url": feed_url,
        "episode_mp3_url": rewrite_host(r_mp3.get("url"), origin, base),
        "episode_wav_url": rewrite_host(r_wav.get("url"), origin, base),
        "cover_url": f"{base}/{c['key_cover']}",
        "public_base": base,
        "episode_count": len(eps),
        "duration_hms": dur_hms,
        "local_rss": RSS_LOCAL,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

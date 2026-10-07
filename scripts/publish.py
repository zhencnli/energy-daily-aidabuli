#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Energy Daily - 播客发布器（支持 Brief / Insight 双栏目）

功能:
  1. 上传音频 (mp3 / wav) 或视频到腾讯云 COS
  2. 维护 data/episodes.json（按期累积，同日 + 同栏目 幂等覆盖）
  3. 渲染符合 Apple Podcasts / 小宇宙 规范的 RSS 2.0 feed
     - 每条 <item> 带 <aili:column> 自定义标签，客户端可据此自动区分布栏
     - 每条可带独立封面 <itunes:image> 与 per-item <itunes:category>
  4. 上传 RSS 与封面图到 COS，输出可填入播客平台的托管地址

用法:
  python publish.py --mp3 audio/xxx.mp3 --title "..." --summary "..." \
                    --date 2026-10-08 --column insight --duration 1320

  --dry-run      只生成本地 RSS，不上传
  --rss-only     不传音频、不改 episodes.json，仅按现有台账重建并上传 RSS
  --image        可选：本集独立封面图（会与 channel 封面区分）

输出: 一行/一段 JSON (ok / feed_url / episode_url / count)
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

DEFAULT_COLUMN = "brief"


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
    s = str(v if v is not None else default).strip().lower()
    if s in ("true", "yes", "y", "1", "explicit"):
        return "true"
    if s in ("false", "no", "n", "0", "clean", "none"):
        return "false"
    return default


def cos_origin(env):
    bucket = env.get("TENCENT_COS_BUCKET", "")
    region = env.get("TENCENT_COS_REGION", "")
    return f"https://{bucket}.cos.{region}.myqcloud.com" if bucket else ""


def public_base(cfg, env):
    b = (cfg.get("cos", {}).get("public_base") or "").strip().rstrip("/")
    return b or cos_origin(env)


def rewrite_host(url, origin, target):
    if not url or not origin or not target or origin == target:
        return url
    return target + url[len(origin):] if url.startswith(origin) else url


def guess_enclosure_type(url):
    u = (url or "").lower()
    if u.endswith(".wav"):
        return "audio/wav"
    if u.endswith(".m4a"):
        return "audio/mp4"
    if u.endswith(".ogg"):
        return "audio/ogg"
    if u.endswith(".mp3"):
        return "audio/mpeg"
    return "audio/mpeg"


def hms(sec):
    sec = int(sec or 0)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def render_rss(cfg, episodes, env):
    p = cfg["podcast"]
    base = public_base(cfg, env) or "https://example.com"
    origin = cos_origin(env)
    feed_url = f"{base}/{cfg['cos']['key_rss']}"
    cover_url = f"{base}/{cfg['cos']['key_cover']}"
    site = p.get("website") or feed_url
    cols = cfg.get("columns", {}) or {}

    by_date = sorted(episodes, key=lambda e: e["date"], reverse=True)
    n = len(by_date)
    items = []
    for idx, e in enumerate(by_date):
        dt = datetime.strptime(e["date"], "%Y-%m-%d").replace(tzinfo=CST)
        ep_no = n - idx
        col = e.get("column", DEFAULT_COLUMN)
        col_cfg = cols.get(col, {})
        img = e.get("image") or cover_url
        cat = col_cfg.get("itunes_category") or p.get("category", "Business")
        sub = col_cfg.get("itunes_subcategory") or p.get("subcategory", "")
        item_cat = f'    <itunes:category text="{esc(cat)}"/>' + (
            f'\n      <itunes:category text="{esc(sub)}"/>' if sub else "")
        items.append(f"""    <item>
      <title>{esc(e['title'])}</title>
      <description><![CDATA[{e.get('summary', '')}]]></description>
      <itunes:summary><![CDATA[{e.get('summary', '')}]]></itunes:summary>
      <itunes:subtitle>{esc(e.get('subtitle', '新能源每日播报'))}</itunes:subtitle>
      <aili:column>{esc(col)}</aili:column>
      <enclosure url="{esc(rewrite_host(e['url'], origin, base))}" length="{e.get('length', 0)}" type="{guess_enclosure_type(e['url'])}"/>
      <itunes:image href="{esc(img)}"/>
      <guid isPermaLink="false">energydaily-{e['date']}-{esc(col)}</guid>
      <pubDate>{format_datetime(dt)}</pubDate>
      <itunes:duration>{e.get('duration_hms', '00:00')}</itunes:duration>
      <itunes:episode>{ep_no}</itunes:episode>
      <itunes:episodeType>full</itunes:episodeType>
      <itunes:explicit>{norm_explicit(p.get('explicit'))}</itunes:explicit>
      <link>{esc(site)}</link>
{item_cat}
    </item>""")

    sub = (p.get("subtitle") or "").strip()
    _sub = f"    <itunes:subtitle>{esc(sub)}</itunes:subtitle>" if sub else ""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:aili="https://aili.agent.qq.com/ns/podcast"
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


def publish_episode(audio_path=None, mp3_path=None, wav_path=None, title=None, summary="", subtitle=None,
                    date=None, duration=0.0, column=DEFAULT_COLUMN, image_path=None,
                    dry_run=False, cfg=None):
    """核心发布逻辑：上传文件 -> 追加 episode -> 渲染 RSS -> 上传 RSS。
    返回 dict（含 episode_url / feed_url / episode_count）。供 CLI 与 studio.py 共用。
    """
    cfg = cfg or load_config()
    c = cfg["cos"]
    env = dict(os.environ)
    env.update(load_env())

    if not date:
        date = datetime.now(CST).strftime("%Y-%m-%d")

    audio_path = audio_path or mp3_path or wav_path
    if not audio_path or not os.path.exists(audio_path):
        return {"ok": False, "error": "缺少音频/视频文件"}
    if not title:
        return {"ok": False, "error": "缺少标题"}

    base_name = cfg["naming"]["audio_basename"].format(date=date)
    ext = os.path.splitext(audio_path)[1].lower()
    # 视频用 video/mp4；音频按扩展名
    ctype = {"mp4": "video/mp4", "mov": "video/quicktime", "m4a": "audio/mp4",
             "wav": "audio/wav", "mp3": "audio/mpeg", "ogg": "audio/ogg"}.get(ext, "application/octet-stream")
    key_audio = f"{c['prefix_audio']}/{base_name}{ext}"

    # 1) 上传音频/视频
    r_audio = cos(cfg, ["upload", "--file", audio_path, "--key", key_audio,
                       "--content-type", ctype], dry_run)
    if not r_audio.get("ok"):
        return {"ok": False, "stage": "upload-audio", "detail": r_audio}

    # 2) 上传本集独立封面（可选）
    img_url = None
    if image_path and os.path.exists(image_path):
        ik = c.get("prefix_meta", "energy-daily/meta")
        img_ext = os.path.splitext(image_path)[1].lower()
        key_img = f"{ik}/{base_name}-cover{img_ext}"
        r_img = cos(cfg, ["upload", "--file", image_path, "--key", key_img,
                         "--content-type", ("image/png" if img_ext == ".png" else "image/jpeg")], dry_run)
        if r_img.get("ok"):
            img_url = r_img.get("url")

    # 3) 维护 episodes 台账（同日 + 同栏目 幂等覆盖）
    eps = [e for e in load_episodes()
           if not (e.get("date") == date and e.get("column", DEFAULT_COLUMN) == column)]
    eps.append({
        "date": date,
        "column": column,
        "title": title,
        "subtitle": subtitle or (cfg.get("columns", {}).get(column, {}).get("label", "")),
        "summary": summary,
        "url": r_audio.get("url") if not dry_run else f"{base_name}{ext}",
        "length": r_audio.get("size", 0),
        "duration_hms": hms(duration),
        "duration_sec": duration,
        "image": (rewrite_host(img_url, cos_origin(env), public_base(cfg, env))
                  if img_url and not dry_run else None),
        "wav_url": None,
    })
    eps.sort(key=lambda e: e["date"])
    if not dry_run:
        save_episodes(eps)

    # 4) 渲染 RSS
    xml = render_rss(cfg, eps, env)
    os.makedirs(os.path.dirname(RSS_LOCAL), exist_ok=True)
    with open(RSS_LOCAL, "w", encoding="utf-8") as fh:
        fh.write(xml)

    # 5) 上传 RSS
    r_rss = cos(cfg, ["upload", "--file", RSS_LOCAL, "--key", c["key_rss"],
                      "--content-type", "application/rss+xml; charset=utf-8"], dry_run)
    if not r_rss.get("ok"):
        return {"ok": False, "stage": "upload-rss", "detail": r_rss}

    base = public_base(cfg, env)
    origin = cos_origin(env)
    feed_url = rewrite_host(r_rss.get("url"), origin, base) or f"{base}/{c['key_rss']}"
    return {
        "ok": True,
        "date": date,
        "column": column,
        "feed_url": feed_url,
        "episode_url": rewrite_host(r_audio.get("url"), origin, base),
        "image_url": (rewrite_host(img_url, origin, base) if img_url else None),
        "public_base": base,
        "episode_count": len(eps),
        "duration_hms": hms(duration),
        "local_rss": RSS_LOCAL,
    }


def rebuild_rss_only(dry_run=False, cfg=None):
    cfg = cfg or load_config()
    env = dict(os.environ)
    env.update(load_env())
    eps = load_episodes()
    xml = render_rss(cfg, eps, env)
    tmp = os.path.join(os.environ.get("TEMP", "/tmp"), "podcast-rebuild.xml")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(xml)
    r = cos(cfg, ["upload", "--file", tmp, "--key", cfg["cos"]["key_rss"],
                  "--content-type", "application/rss+xml; charset=utf-8"], dry_run)
    if not r.get("ok"):
        return {"ok": False, "stage": "upload-rss", "detail": r}
    return {"ok": True, "rss_only": True,
            "feed_url": f"{public_base(cfg, env)}/{cfg['cos']['key_rss']}",
            "episode_count": len(eps)}


def main():
    cfg = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--mp3")
    ap.add_argument("--wav", default=None)
    ap.add_argument("--title")
    ap.add_argument("--summary", default="")
    ap.add_argument("--subtitle", default=None)
    ap.add_argument("--date", help="YYYY-MM-DD")
    ap.add_argument("--duration", type=float, default=0.0, help="秒")
    ap.add_argument("--column", default=DEFAULT_COLUMN, choices=["brief", "insight"])
    ap.add_argument("--image", default=None, help="本集独立封面图路径")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rss-only", action="store_true",
                    help="不传音频、不改 episodes.json，仅按现有台账重建并上传 RSS")
    args = ap.parse_args()

    env = dict(os.environ)
    env.update(load_env())

    if args.rss_only:
        res = rebuild_rss_only(args.dry_run, cfg)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    audio = args.mp3 or args.wav
    for missing in ("title", "date"):
        if not getattr(args, missing):
            ap.error(f"缺少 --{missing}（--rss-only 模式才不需要）")

    res = publish_episode(
        mp3_path=args.mp3, wav_path=args.wav, title=args.title,
        summary=args.summary, subtitle=args.subtitle, date=args.date,
        duration=args.duration, column=args.column, image_path=args.image,
        dry_run=args.dry_run, cfg=cfg,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())

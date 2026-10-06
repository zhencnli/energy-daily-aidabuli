#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI答不锂 · Energy Daily —— 云端编排主控（GitHub Actions 版）

流程:
  1. 幂等检查   —— COS 上当天 MP3 已存在则直接跳过（双 cron 兜底的关键）
  2. 搜集       —— Tavily Search API（或 RSS 兜底）
  3. 生成       —— DeepSeek（OpenAI 兼容接口）产出图文 + 口播稿 + 英语词
  4. 合成       —— 复用本地已验证的 scripts/tts_edge.py  (edge-tts -> MP3 + WAV)
  5. 发布       —— 复用本地已验证的 scripts/publish.py   (上传 COS + 重生 RSS)

环境变量:
  TENCENT_COS_SECRET_ID / TENCENT_COS_SECRET_KEY / TENCENT_COS_REGION / TENCENT_COS_BUCKET
  LLM_API_KEY (默认 DeepSeek) / LLM_BASE_URL / LLM_MODEL
  SEARCH_PROVIDER = tavily | rss | none
  TAVILY_API_KEY

用法:
  python scripts/pipeline.py [--date YYYY-MM-DD] [--force] [--dry-run]
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("缺少 requests，请先 pip install -r requirements.txt")

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
REPORT = ROOT / "report"
AUDIO = ROOT / "audio"
DATA = ROOT / "data"
CONFIG = ROOT / "config.json"
WORDS = DATA / "english_words.json"
CST = timezone(timedelta(hours=8))

# 五大板块检索词 —— 与本机 Step 1 的口径保持一致
SECTOR_QUERIES = [
    ("风电", "风力发电 海上风电 中标 并网 整机 叶片 海缆 政策"),
    ("光伏", "光伏 组件 电池片 硅料 逆变器 分布式 招标 关税 UFLPA"),
    ("储能", "储能 招标 中标 锂电池 钠电池 液流电池 构网型储能 新型储能"),
    ("AIDC", "AI数据中心 智算中心 绿电直连 源网荷储 PPA 液冷 用电负荷"),
    ("新能源", "氢能 绿氢 充换电 绿电交易 碳市场 电力市场"),
    ("Global", "renewable energy solar wind storage data center PPA policy"),
]


def log(msg):
    print(f"[pipeline] {msg}", flush=True)


def today_str():
    return datetime.now(CST).strftime("%Y-%m-%d")


def load_cfg():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def cos_audio_url(date):
    b = os.environ.get("TENCENT_COS_BUCKET", "")
    r = os.environ.get("TENCENT_COS_REGION", "")
    return f"https://{b}.cos.{r}.myqcloud.com/energy-daily/audio/energydaily-{date}.mp3"


def already_published(date):
    """幂等守卫: COS 上当天 MP3 是否已存在。HEAD 足够, 不受强制下载头影响。"""
    try:
        resp = requests.head(cos_audio_url(date), timeout=20, allow_redirects=True)
        return resp.status_code == 200
    except Exception as exc:
        log(f"幂等检查失败(按未发布处理): {exc}")
        return False


# ---------------------------------------------------------------- 搜集
def tavily_search(query, max_results=8):
    key = os.environ.get("TAVILY_API_KEY", "")
    if not key:
        return []
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": key,
                "query": query,
                "topic": "news",
                "search_depth": "basic",
                "max_results": max_results,
                "days": 1,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json().get("results") or []
    except Exception as exc:
        log(f"Tavily 检索失败 [{query[:20]}]: {exc}")
        return []


def collect_news(provider):
    """返回候选新闻列表，已按 url 去重。"""
    if provider == "none":
        return []
    if provider == "rss":
        return collect_rss()

    found, seen = [], set()
    for sector, q in SECTOR_QUERIES:
        for item in tavily_search(q):
            url = (item.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            found.append({
                "sector": sector,
                "title": (item.get("title") or "").strip(),
                "url": url,
                "source": item.get("source") or "",
                "published": item.get("published_date") or "",
                "content": (item.get("content") or "")[:800],
            })
        time.sleep(0.4)
    return found


RSS_SOURCES = [
    # 可按需增补。注意：网页/RSS 改版会失效，需定期维护。
    "https://www.pv-magazine.com/feed/",
    "https://www.energy-storage.news/feed/",
    "https://www.datacenterdynamics.com/rss/",
]


def collect_rss():
    """零成本兜底: 直接抓 RSS。覆盖度不如搜索 API，但无需任何密钥。"""
    import xml.etree.ElementTree as ET

    items = []
    for url in RSS_SOURCES:
        try:
            resp = requests.get(url, timeout=45,
                                headers={"User-Agent": "Mozilla/5.0 EnergyDailyBot"})
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception as exc:
            log(f"RSS 抓取失败 {url}: {exc}")
            continue
        for it in (root.iter("item") if root.tag != "item" else [root]):
            link = (it.findtext("link") or "").strip()
            if not link:
                continue
            items.append({
                "sector": "未分类",
                "title": (it.findtext("title") or "").strip(),
                "url": link,
                "source": url,
                "published": it.findtext("pubDate") or "",
                "content": re.sub(r"<[^>]+>", "", it.findtext("description") or "")[:600],
            })
    return items


# ---------------------------------------------------------------- 大模型
def call_llm(messages, max_tokens=None, retries=3, json_mode=True):
    base = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
    key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "deepseek-v4-flash")
    if not key:
        sys.exit("缺少 LLM_API_KEY")
    if max_tokens is None:
        max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "32000"))

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    last = None
    for attempt in range(retries):
        try:
            resp = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json=payload,
                timeout=300,
            )
            resp.raise_for_status()
            choice = resp.json()["choices"][0]
            return choice["message"]["content"], choice.get("finish_reason", "")
        except Exception as exc:
            last = exc
            log(f"LLM 第 {attempt + 1} 次失败: {exc}")
            time.sleep(5 * (attempt + 1))
    sys.exit(f"LLM 调用最终失败: {last}")


def extract_json(text, finish_reason=""):
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j < 0:
        if finish_reason == "length":
            sys.exit("LLM 输出被 max_tokens 截断（连一个完整的 JSON 对象都没生成）。"
                     "提高 Secret LLM_MAX_TOKENS，或减少单次喂给模型的候选条数。")
        sys.exit(f"LLM 未返回 JSON（finish_reason={finish_reason!r}）。"
                 f"输出前 300 字符：{text[:300]!r}")
    try:
        return json.loads(text[i:j + 1])
    except Exception as exc:
        if finish_reason == "length":
            sys.exit(f"LLM 输出被 max_tokens 截断，JSON 不完整：{exc}")
        sys.exit(f"JSON 解析失败：{exc}；输出尾部 300 字符：{text[-300:]!r}")


SYSTEM_PROMPT = """你是「AI答不锂」新能源行业每日简报的主笔主播老锂。风格：专业、务实、说人话、不夸大。
严格遵守：所有事实必须来自提供的检索结果，不得杜撰任何数据、金额、公司名或链接；无法核实的宁可不写。
所有输出必须是合法 JSON。"""


def build_prompt(date, news, used_words):
    news_block = "\n".join(
        f"[{i}] 板块={n['sector']} 来源={n['source']} 时间={n['published']}\n"
        f"标题: {n['title']}\n链接: {n['url']}\n摘要: {n['content']}\n"
        for i, n in enumerate(news, 1)
    ) or "（本次未检索到候选新闻）"

    return f"""今天是 {date}。以下是过去 24 小时检索到的新能源行业候选动态（可能夹杂旧文，请核对时间）：

{news_block}

请产出严格符合下列 JSON Schema 的结果（不要输出任何解释文字）：

{{
  "title": "期标题，格式：Energy Daily · {date}｜今日新能源头条",
  "subtitle": "风电 · 光伏 · 储能 · AIDC · 新能源",
  "summary": "本期 3-5 条最核心要点的串联，≤500 字，保留关键数据/金额/规模，不得引入候选之外的事实",
  "report_md": "完整图文简报 Markdown。第一行必须是「AI答不锂，我是小AI」；第二行用括号注明覆盖时间窗口；随后按重要性排序 8-10 条动态，每条含：板块标注、标题、2-4 句要点摘要、信息来源、发布时间、可点击原文链接；末尾附今日英语单词板块（含英式/美式音标、词性、中英双语释义、至少 2 个例句）。不足 8 条就如实少写，严禁凑数。",
  "script_txt": "口播稿纯文本，禁止 Markdown/链接。开场词必须是「爱答不理，我是小爱。现在是{date} 早上8点」；随后逐条播报标题 + 一句核心要点；结束语「以上就是今天的「Energy Daily」，感谢收听，See you tomorrow!」。字数控制在 1500-2000 汉字。",
  "word": "今日英语单词（字符串），不得是这个已用词列表中的任何一个: {json.dumps(used_words, ensure_ascii=False)}"
}}

注意：JSON 内所有字符串需要正确转义换行符 \\n。"""


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=today_str())
    ap.add_argument("--force", action="store_true", help="即使当天已发布也重新生成")
    ap.add_argument("--dry-run", action="store_true", help="只生成文本，不合成不发布")
    args = ap.parse_args()

    date = args.date
    cfg = load_cfg()
    tts_cfg = cfg.get("tts", {})
    blog_title = cfg["podcast"]["title"]

    log(f"目标日期: {date} (Asia/Shanghai)")

    # 幂等守卫：dry-run 不写 COS，无需绕过，直接放行即可
    if not args.dry_run and not args.force and already_published(date):
        log("当天 MP3 已存在于 COS，跳过（幂等）。需要重跑请加 --force")
        return 0

    # 1) 搜集
    provider = os.environ.get("SEARCH_PROVIDER", "tavily")
    if provider == "tavily" and not os.environ.get("TAVILY_API_KEY", ""):
        log("⚠️  未配置 TAVILY_API_KEY，检索将返回空；"
            "建议把 Secret SEARCH_PROVIDER 设为 rss 免费用 RSS 兜底")
    news = collect_news(provider)
    log(f"检索到候选 {len(news)} 条（provider={provider}）")
    if not news:
        log("警告：候选为空，仍能生成但内容质量会下降")

    # 2) 已用单词台账
    used_words = []
    if WORDS.exists():
        try:
            used_words = json.loads(WORDS.read_text(encoding="utf-8"))
        except Exception:
            used_words = []

    # 3) 生成
    raw, finish = call_llm([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_prompt(date, news, used_words)},
    ])
    log(f"LLM 返回 {len(raw)} 字符，finish_reason={finish!r}")
    if finish == "length":
        sys.exit("LLM 输出被截断，本期放弃发布（不会产出半成品）")
    data = extract_json(raw, finish)
    for key in ("title", "summary", "report_md", "script_txt", "word"):
        if not data.get(key):
            sys.exit(f"LLM 输出缺少字段: {key}")

    REPORT.mkdir(parents=True, exist_ok=True)
    AUDIO.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)

    md_path = REPORT / f"energydaily-{date}.md"
    script_path = REPORT / f"energydaily-{date}.script.txt"
    md_path.write_text(data["report_md"], encoding="utf-8")
    script_path.write_text(data["script_txt"], encoding="utf-8")
    log(f"图文 {md_path.name} ({len(data['report_md'])} 字) / 口播稿 {script_path.name} ({len(data['script_txt'])} 字)")

    word = data["word"].strip()
    if word and word not in used_words:
        used_words.append(word)
        WORDS.write_text(json.dumps(used_words, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"英语单词已入台账: {word} (累计 {len(used_words)})")

    if args.dry_run:
        log("--dry-run：跳过合成与发布")
        return 0

    # 4) 合成（注意：负速率必须用等号传参）
    mp3 = AUDIO / f"energydaily-{date}.mp3"
    wav = AUDIO / f"energydaily-{date}.wav"
    cmd = [
        sys.executable, str(SCRIPTS / "tts_edge.py"),
        "--text-file", str(script_path),
        "--out-wav", str(wav),
        "--out-mp3", str(mp3),
        "--voice", tts_cfg.get("voice", "zh-CN-XiaoxiaoNeural"),
        f"--rate={tts_cfg.get('rate', '-6%')}",
        "--title", data["title"],
        "--artist", cfg["podcast"].get("author", "老锂"),
        "--album", blog_title,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
    if proc.returncode != 0:
        sys.exit(f"TTS 失败: {(proc.stderr or '')[:500]}")
    tts_out = json.loads(proc.stdout.strip().splitlines()[-1])
    duration = tts_out.get("duration_sec", 0)
    log(f"合成完成: {tts_out.get('duration_hms')} / {tts_out.get('chars_per_min')} 字每分钟")

    # 5) 发布（上传 MP3+WAV 到 COS -> 更新 episodes.json -> 重生 RSS -> 上传 RSS）
    pub = subprocess.run(
        [sys.executable, str(SCRIPTS / "publish.py"),
         "--mp3", str(mp3), "--wav", str(wav),
         "--title", data["title"],
         "--subtitle", data.get("subtitle", "风电 · 光伏 · 储能 · AIDC · 新能源"),
         "--summary", data["summary"],
         "--date", date, "--duration", str(duration)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
    )
    if pub.returncode != 0:
        sys.exit(f"发布失败: {(pub.stderr or '')[:800]}")
    try:
        result = json.loads(pub.stdout.strip().splitlines()[-1])
    except Exception:
        sys.exit(f"发布输出解析失败: {pub.stdout[:400]}")

    log("=" * 60)
    log(f"✅ 发布成功  {date}")
    log(f"   feed    : {result.get('feed_url')}")
    log(f"   episode : {result.get('episode_mp3_url')}")
    log(f"   episodes: {result.get('episode_count')} 期 / 时长 {result.get('duration_hms')}")
    log("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())

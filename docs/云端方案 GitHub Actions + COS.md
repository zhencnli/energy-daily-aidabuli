# AI答不锂 · 云端方案详解：GitHub Actions（计算）+ 腾讯云 COS（托管）

> 编制日期：2026-10-06
> 目标：彻底摆脱对这台 MateBook 的依赖，每天 07:30 前自动出一期并更新播客 RSS

---

## 〇、先说现状：本机方案其实已经跑通了

在展开之前，先报告一个已验证的事实 —— **今天（10-06）07:00 的定时任务已完整执行**：

| 产物 | 本地时间 | 状态 |
| --- | --- | --- |
| `report/energydaily-2026-10-06.md`（14.9 KB） | 07:11 | ✅ |
| `report/energydaily-2026-10-06.script.txt`（4.6 KB） | 07:11 | ✅ |
| `audio/energydaily-2026-10-06.mp3`（2.66 MB） | 07:12 | ✅ |
| COS 上的 RSS feed 已含第 2 期、MP3 可下载（2,656,425 B） | 07:25 后 | ✅ |

昨天把「休眠超时」从 3 小时改成「从不」这一项修改是决定性的 —— 机器整夜在线，任务按时触发。所以本机方案**目前是有效的**；是否要迁到云端，取决于你对"电脑必须一直开着"这件事的容忍度。

---

## 一、架构：谁负责什么

```
[GitHub Actions timer (UTC)]
        │  cron，每天自动触发（含一次兜底重试）
        ▼
[GitHub-hosted Runner: ubuntu-latest]  ← 计算，免费额度内的部分为 ¥0
        │
        ├─ ① 搜索：Tavily Search API（或改为抓取固定信源）
        ├─ ② 写作：DeepSeek API（V4-Flash 为主）
        ├─ ③ 合成：edge-tts → MP3 + WAV（沿用已验证的 tts_edge.py）
        ├─ ④ 发布：cos-nodejs-sdk-v5 → 上传音频 + 重生 RSS（沿用 publish.py）
        └─ ⑤ 状态：git commit & push（episodes.json / english_words.json / report）
        │
        ▼
[腾讯云 COS ap-shanghai]  ← 存储与托管，按量付费
        ├─ energy-daily/audio/*.mp3      （RSS enclosure）
        ├─ energy-daily/podcast.xml      （订阅源）
        └─ energy-daily/cover.jpg        （封面）
        │
        ▼
   小宇宙 / Apple Podcasts 自动拉取
```

**分工原则**：GitHub 只做「会消失的事」（计算），COS 只做「要留存的事」（文件），Git 仓库做「状态」（元数据与归档）。三者都用免费额度打底。

---

## 二、为什么这套组合最划算

| 环节 | 选择 | 理由 |
| --- | --- | --- |
| 编排与计算 | GitHub Actions | 私有仓库 2,000 Linux 分钟/月免费；本项目约 150–450 分钟，绰绰有余。单 job 上限 6 小时，**不受云函数 900 秒天花板限制** |
| 模型 | DeepSeek | 07:00 属**空闲时段**，天然半价；V4-Flash 足够胜任筛选与改写 |
| 语音合成 | edge-tts | ¥0，音色 `zh-CN-XiaoxiaoNeural` 已验证（想换成合规的腾讯云 TTS 只需 ¥1.4/月） |
| 存储/托管 | 腾讯云 COS | 你已有桶；标准存储与流量都有免费额度；生态无缝 |
| 状态 | Git 仓库 | 天然持久化、可回滚、可审计，且完全免费 |

---

## 三、成本测算

### 3.1 基础单价（均为官方现行价，2026-10 核）

| 资源 | 单价 | 备注 |
| --- | --- | --- |
| GitHub Actions（私有仓，超额） | **$0.006 / Linux 分钟** | Free 计划含 2,000 分钟/月；公开仓库无限免费 |
| GitHub 存储（超额） | 约 $0.008/GB/天（或 $0.25/GB·月口径） | Free 含 500 MB artifact + 10 GB cache/仓库 |
| DeepSeek V4-Flash（**空闲时段**） | 输入 ¥1.5 / 输出 ¥4.5 每百万 token；缓存命中 ¥0.05 | 高峰（9–12、14–18 点）翻倍 |
| DeepSeek V4-Pro（空闲） | 输入 ¥4.5 / 输出 ¥13.5；缓存命中 ¥0.15 | 备选 |
| COS 标准存储 | **¥0.118 / GB / 月** | 免费额度 50 GB/月 |
| COS 外网下行流量（上海） | **¥0.6 / GB** | 这是唯一随听众线性增长的成本 |
| COS 请求 | 读 ¥0.01 / 万次 | 可忽略 |
| Tavily Search API | 有免费额度（约 1,000 次/月） | 超额后按次计费，需查实时价 |

### 3.2 每月用量估算

| 项目 | 用量 | 费用 |
| --- | --- | --- |
| Actions 运行时长 | 每期约 5–15 分钟（开 pip 缓存后可压到 4–6 分钟）× 30 期 ≈ **150–450 分钟** | **¥0**（在 2,000 分钟免费额度内；即使超额也仅约 $1.8） |
| LLM | 每期约 60 k 输入 + 6 k 输出 | **¥3.6**（V4-Flash）／¥10.5（V4-Pro） |
| TTS | edge-tts | **¥0** |
| COS 存储 | MP3 2.6 MB × 30 ≈ 0.08 GB/月（建议 WAV 不再上传） | **≈ ¥0.01**（免费额度内） |
| COS 流量 | 见下表 | **¥0 – ¥16** |
| 搜索 API | 每天 5–8 次查询 ≈ 200 次/月 | **¥0**（免费额度内） |

### 3.3 流量（唯一变量）

上海地域 0.6 元/GB，单期 MP3 2.6 MB：

| 月下载次数 | 流量 | 费用 |
| --- | --- | --- |
| < 3,800 | < 10 GB | **¥0**（免费额度内） |
| 10,000 | 26 GB | ¥15.6 |
| 50,000 | 130 GB | ¥78 |
| 100,000 | 260 GB | ¥156 |

### 3.4 月度总账

| 阶段 | 总成本 | 构成 |
| --- | --- | --- |
| **起步期**（月下载 < 1,000） | **¥4 – 15 / 月** | LLM ¥3.6–10.5，其余≈0 |
| 成长期（1 万次） | **¥20 – 30 / 月** | 流量成为第二大头 |
| 规模期（10 万次） | **¥170 – 200 / 月** | 流量主导，届时应改挂 CDN |

> 对比参考：昨天测过的「腾讯云函数 SCF」路线，计算也是 ≈¥0，但受 900 秒超时限制需要拆分函数；「轻量云服务器」路线约 ¥10–30/月但改造量最小。综合改造成本，**私有仓库 GitHub Actions 是当前最优解**。

---

## 四、准备清单（你要做的事，约 40 分钟）

### 4.1 账号与密钥

| # | 项目 | 获取方式 | 存放位置 |
| --- | --- | --- | --- |
| 1 | GitHub 账号 | github.com 注册 | — |
| 2 | 新建仓库（**建议 Private**） | New repository | — |
| 3 | `DEEPSEEK_API_KEY` | platform.deepseek.com → API Keys | 仓库 Settings → Secrets → Actions |
| 4 | `TAVILY_API_KEY` | tavily.com 注册（免费额度） | 同上（**可选**，见 §4.4） |
| 5 | `TENCENT_COS_SECRET_ID` / `SECRET_KEY` | 腾讯云控制台 → 访问密钥 | 同上 |
| 6 | `TENCENT_COS_BUCKET` = `aili-1500638180` | 已有 | 同上（也可写死在 config.json） |
| 7 | `TENCENT_COS_REGION` = `ap-shanghai` | 已有 | 同上 |

> ⚠️ **强烈建议先轮换 COS 密钥**：当前这对 SecretKey 曾在对话里明文出现过，且旧 key 仍在被本机定时任务使用。请在控制台新建一对专供 GitHub 使用的子账号密钥（只给 COS 读写权限），轮换后再停用旧的。

### 4.2 仓库内容

把现有 `energy-daily` 目录改造成仓库，结构如下（我已在 `cloud/repo/` 下备好新增部分）：

```
your-repo/
├── .github/workflows/daily.yml   ← 新增（已提供）
├── requirements.txt              ← 新增（已提供）
├── scripts/
│   ├── pipeline.py               ← 新增（已提供，云端编排主控）
│   ├── tts_edge.py               ← 沿用本地已验证版本
│   ├── publish.py                ← 沿用本地已验证版本（已支持变量化域名）
│   ├── cos_upload.mjs            ← 沿用（本身优先读环境变量，天然适配 Actions）
│   └── make_cover.py             ← 沿用（封面一般不再需要每天重跑）
├── assets/cover.jpg              ← 沿用
├── config.json                   ← 沿用，但需把 node_bin 改为 "node"（workflow 会自动改）
├── data/episodes.json            ← 沿用（作为状态初值；之后由 Actions 自动提交更新）
├── data/english_words.json       ← 沿用
└── report/                       ← 历史图文，Actions 会自动追加新的一期
```

### 4.3 必须做的两处适配

1. **`config.json` 的 `node_bin`**：本地是绝对路径，云端应为 `node`。workflow 里已用一行 Python 自动改写，无需手工改。
2. **时区**：GitHub cron 用的是 **UTC**。北京时间 07:00 = UTC 23:00（前一天）。考虑到 GitHub cron 在整点压力大、可能延迟 5–30 分钟，我把触发提前到 **UTC 22:37（北京 06:37）**，并加一次 **UTC 23:25（北京 07:25）** 的幂等兜底。

### 4.4 关于「搜索」这一环（重要）

本机方案用的是 WorkBuddy 内置的 WebSearch，**云端没有这个工具**。三种替代：

| 方式 | 成本 | 覆盖度 | 稳定性 |
| --- | --- | --- | --- |
| **Tavily Search API**（推荐） | 免费额度内 ¥0 | 好 | 高 |
| 抓取固定信源 RSS/网页 | ¥0 | 取决于源 | 中（网页改版会失效） |
| 改用自带联网能力的模型 | 视模型 | 好 | 中 |

建议先用 Tavily + 备用 RSS 源双保险；`pipeline.py` 里已内建这两种模式（环境变量 `SEARCH_PROVIDER`）。

---

## 五、运行流程（workflow 每天做什么）

1. `checkout` 拉仓库（含历史 `episodes.json` / `english_words.json`）
2. `setup-python` + pip 缓存安装依赖
3. `npm i cos-nodejs-sdk-v5`
4. 改写 `config.json` 的 `node_bin` 为 `node`
5. **幂等检查**：HEAD 请求 COS 上当天 MP3，存在且未加 `--force` → 直接退出（防重复）
6. `pipeline.py`：搜索 → LLM 生成图文/口播稿/英语词 → 调用 `tts_edge.py` → 调用 `publish.py`
7. 失败重试：LLM 调用内置 3 次退避重试
8. `git commit & push` 回写 `data/` 与 `report/`（每天一个 commit）
9. 上传 `report` + `mp3` 为 artifact（保留 3 天，便于排查；注意别超 500 MB 存储额度）
10. 失败时 Actions 会发邮件通知

---

## 六、风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| GitHub cron 偶发延迟/漏跑 | 当天缺更 | 双 cron + 幂等兜底；开启后观察 1 周实际触发时间 |
| `edge-tts` 是非官方接口 | 长期商用有不确定性 | 已预留切换腾讯云 TTS（¥1.4/月）的路径 |
| LLM 输出格式不稳定 | 生成失败 | `pipeline.py` 内置 JSON 提取容错 + 3 次重试 + 失败即 exit 非零（不会发布半成品） |
| 密钥泄露 | 安全风险 | 全部走 GitHub Secrets，掩码显示；建议给 GitHub 单独建一个只授权 COS 的子账号密钥 |
| 内容质量下滑（云端模型≠WorkBuddy 内置） | 稿件质量 | 前 7 天并行运行本机 + 云端，逐条比对后再切换 |
| 500 MB artifact 存储超限 | 额外扣费 | artifact 仅保留 3 天；音频最终以 COS 为准 |

---

## 七、迁移步骤（建议三天并行）

**Day 1**：建私有仓库 → 灌 `Secrets` → 推代码 → 手动 `workflow_dispatch` 跑一次，看日志是否与本机产物一致。
**Day 2**：保持本机自动化开启，同时让云端定时跑；**本机仍是唯一发布源**（云端先只跑 pipeline 的 `--dry-publish` 模式）。
**Day 3**：确认云端产物质量达标 → 关闭本机两个 automation → 云端完全接管。

---

## 八、一句话总结

**每月现金成本约 ¥4–15（起步期），一次性准备约 40 分钟 + 半天代码适配。换来的是：电脑可以关机、可以带走、可以断电，节目照常更新。**

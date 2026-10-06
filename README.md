# AI答不锂 · Energy Daily（云端版）

GitHub Actions 负责计算，腾讯云 COS 负责托管，Git 仓库负责状态。
每天北京时间 06:37 自动出一期（07:25 有一次幂等兜底），07:30 前更新播客 RSS。

**RSS（填给小宇宙 / Apple Podcasts 一次即可）**
```
https://aili-1500638180.cos.ap-shanghai.myqcloud.com/energy-daily/podcast.xml
```

---

## 目录说明

| 路径 | 作用 |
| --- | --- |
| `.github/workflows/daily.yml` | 定时编排：装依赖 → 跑 pipeline → 回写状态 |
| `scripts/pipeline.py` | 云端主控：搜索 → LLM → TTS → 发布（含幂等守卫） |
| `scripts/tts_edge.py` | 文本 → MP3 + WAV（edge-tts，已内嵌封面） |
| `scripts/publish.py` | 上传 COS → 更新 `episodes.json` → 重生 RSS |
| `scripts/cos_upload.mjs` | COS 上传工具（优先读环境变量，无需 `.env`） |
| `scripts/make_cover.py` | 由 logo 生成封面（一般不需每天跑） |
| `config.json` | 品牌、COS 前缀、TTS 参数。注意 `node_bin` 云端为 `node` |
| `data/episodes.json` | 各期元数据，**状态文件**，由 Actions 自动提交 |
| `data/english_words.json` | 已用英语单词台账，避免重复 |
| `report/` | 历史图文与口播稿归档 |

---

## 首次部署（一次性）

### 1. 创建仓库并推送
```bash
git init && git add . && git commit -m "init: energy daily cloud"
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git branch -M main && git push -u origin main
```
建议设为 **Private**（音频虽不入库，但 prompt 与配置属于你的资产）。

### 2. 填 Secrets
仓库 → Settings → Secrets and variables → Actions → New repository secret：

| Name | 必填 | 取值 |
| --- | --- | --- |
| `TENCENT_COS_SECRET_ID` | ✅ | 腾讯云访问密钥 ID |
| `TENCENT_COS_SECRET_KEY` | ✅ | 腾讯云访问密钥 Key（**建议新建专用子账号并轮换旧的**） |
| `TENCENT_COS_REGION` | ✅ | `ap-shanghai` |
| `TENCENT_COS_BUCKET` | ✅ | `aili-1500638180` |
| `DEEPSEEK_API_KEY` | ✅ | platform.deepseek.com 创建 |
| `TAVILY_API_KEY` | 可选 | tavily.com；不填时把 `SEARCH_PROVIDER` 设为 `rss` |
| `SEARCH_PROVIDER` | 可选 | `tavily`（默认）／ `rss` ／ `none` |
| `LLM_MODEL` | 可选 | 默认 `deepseek-v4-flash`；想要更好质量填 `deepseek-v4-pro` |

### 3. 手动验证（务必分两步，别一步到位）

**第 1 步 · 安全冒烟（不碰 COS）**
Actions → Energy Daily → Run workflow →
`force` ✅ 勾、`dry_run` ✅ 勾 → Run。

这一步走「检索 → LLM → 生成图文与口播稿」，**不合成、不上传**。
下载 `energydaily-<run_number>` 里的 `report/*.md`，与本机稿件逐条比对质量。
只要这一步失败（缺少 Secret、网络不通、JSON 解析失败），都不可能影响线上 feed。

**第 2 步 · 真刀真枪**
`dry_run` 确认没问题后，只勾 `force`、**不勾** `dry_run` 再跑一次。
这一步会合成 MP3/WAV 并上传 COS、更新 RSS。

> ⚠️ 当天已经出过一期时，勾选 `force` 会用云端稿件**覆盖**当天那一期。
> 所以第一次跑建议挑「当天还没出刊」的时候，或者接受覆盖。

### 4. 观察与切换
- 前 3 天：本机与云端并行，逐条比对稿件质量
- 确认无误后：关闭本机的两个定时任务，云端完全接管

---

## 幂等与容错

- 每次运行先 HEAD 请求当天 MP3，已存在则退出（双 cron 不会重复发布）
- LLM 调用失败自动重试 3 次（退避），最终失败则非零退出，**不会发布半成品**
- 需要重跑某一天：Actions → Run workflow → 勾选 `force`

## 时区与调度

| cron (UTC) | 北京时间 | 用途 |
| --- | --- | --- |
| `37 22 * * *` | 06:37 | 主触发（GitHub 整点拥堵，提前量留足） |
| `25 23 * * *` | 07:25 | 幂等兜底 |

## 成本

起步期约 **¥4–15/月**：Actions 在 2,000 分钟免费额度内（¥0），DeepSeek 约 ¥3.6，COS 存储与小额流量在免费额度内。
唯一随听众线性增长的是 COS 外网下行流量（0.6 元/GB，月下载 1 万次约 ¥15.6）。

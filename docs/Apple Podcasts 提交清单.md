# Apple Podcasts 提交清单｜AI答不锂

> 生成时间：2026-10-05 21:15 · feed 校验状态：**通过**（W3C Feed Validator：Congratulations! This is a valid RSS feed.）
>
> **2026-10-06 更正（重要）**：上一版称「已部署 Cloudflare Worker 边缘代理」，**经核实该 Worker 并未真正部署成功**（`aidabuli.workers.dev` 实际不可达，返回 502），且当时错误地把 `config.cos.public_base` 指向了这个死地址，导致 feed 内链接一度失效。**现已回滚**：`public_base` 恢复为空，feed 全部链接改回腾讯云 COS 直连（音频/封面用默认域名、主页用静态网站端点），线上已验证所有 URL 200 可达。**当前请用下方 COS 直连地址提交 Apple**。Cloudflare Worker 边缘代理仍是可选优化（见「调整④」），需你在 Cloudflare 真正部署后再切换，未部署前切勿把 feed 指向 workers.dev。

---

## 零、先复制这三段（提交时要粘的关键信息）

**RSS Feed URL（唯一，直接复制）**
```
https://aili-1500638180.cos.ap-shanghai.myqcloud.com/energy-daily/podcast.xml
```

**联系邮箱（Apple 联系你用）**
```
aidabuli@agent.qq.com
```

**节目主页（Apple 会校验）**
```
https://aili-1500638180.cos-website.ap-shanghai.myqcloud.com/
```

---

## 一、提交前置条件（5 分钟，缺一项就会卡住）

按顺序确认，全部打勾再进第二步：

| # | 检查项 | 怎么确认 | 状态 |
| --- | --- | --- | --- |
| 1 | Apple ID 已登录 Podcasts Connect | 打开 https://podcastsconnect.apple.com 能进首页 | ☐ |
| 2 | **Apple ID 已绑定有效付款方式** | 手机：设置 → 顶部 Apple ID → 付款与配送；就算播客免费也必须有 | ☐ |
| 3 | **Terms of Service 已接受** | 登录后会弹协议页，点接受；这一步出错就是通用报错的主因 | ☐ |
| 4 | feed 至少 1 期节目 | 已有 1 期（2026-10-05，6分22秒） | ✅ |
| 5 | 封面符合规范 | 1819×1819 JPEG RGB，325 KB（Apple 要求 1400–3000 px、<500 KB） | ✅ |

> **关于第 2、3 项**：Apple 官方社区大量案例显示，「An error has occurred. Try again later.」绝大多数不是 feed 问题，而是**接受服务条款这一步**在 Apple 后台失败。常见诱因：付款方式缺失、Apple ID 首次使用 Music/iTunes 未完成初始化、该 ID 曾注册过 Apple Developer。

---

## 二、提交操作（按顺序点击，约 10 分钟）

1. 打开 https://podcastsconnect.apple.com ，用已绑定付款方式的 Apple ID 登录。
2. 点右上角 **`+`** → **New Show**。
3. 选 **「Add a show with an RSS feed」**（用 RSS 添加节目）。
4. 粘贴上面的 **RSS Feed URL** → 点 **Add**。
5. Apple 会实时抓取并显示预览：**核对标题是否为「AI答不锂」、主播是否为「老锂」、封面是否显示、是否列出 1 期节目**。← 这一步最关键，有问题就改 feed 重传，别硬往下走。
6. 进入 **Show Information** 页，逐项设置：
   - **Content Rights（内容版权）**：勾选你拥有全部内容权利（音乐/第三方素材均无侵权）。
   - **Contact Information（联系信息）**：填邮箱 `aidabuli@agent.qq.com`。
   - **Availability（地区）**：左侧菜单 → 选**所有国家和地区**（默认即可）。
   - **Distribution（分发）**：打开「公开你的 RSS feed」，让第三方播客 App 也能收录。
   - **Transcripts（字幕）**：选 **Apple 生成**（不用自己做）。
   - **Show Release（上线时间）**：选**立即发布**。
   - **Show Claiming**：建议**勾选禁止他人生成 claim token**。
7. 点 **Save**。
8. 点 **Publish**。Apple 会再校验一次 feed，有警告必须先解决才能发布。

---

## 三、提交后

- **审核周期**：通常 **24–72 小时**，高峰期最长到 7 天。审核结果会发邮件到你 Apple ID 邮箱。
- **等待期间绝对不要改这三样**，任何改动会重置排队：① 封面 ② feed 里的节目标题/简介 ③ 节目名/分类。
- **日常自动任务是安全的**：每天 07:25 只往 feed 里**追加新一期**，不动上述元数据，不会打扰审核。
- 通过后 Apple 会分配一个 **Apple Podcasts ID**，记下来（聊天里发我，我写进 README）。

---

## 四、报「An error has occurred. Try again later.」的排障（2026-10-05 21:22 更新）

### 4.0 本轮新查明的事实

我复现了 Apple 爬虫的两种请求方式，发现 COS 对不同方法的返回头**不一致**：

| 请求方法 | Content-Disposition | x-cos-force-download |
| --- | --- | --- |
| `HEAD`（探测用） | `inline` | 无 |
| **`GET`（Apple 实际用的）** | **`attachment`** | **`true`** |

**Apple 的抓取走 GET，所以它拿到的是「这是一个下载文件」的响应，而不是一个网页订阅源。** 这是整套托管里唯一与「正常播客托管商」不一致的地方。腾讯云官方文档确认：
- 该行为针对 **2024-01-01 之后创建**的存储桶，作用于**默认域名 / 静态网站域名 / 全球加速域名**；
- **对象级设置 `Content-Disposition: inline` 无法覆盖**，强制下载优先级更高；
- 官方给的解法只有两条：**绑自定义域名**（需备案）或**走 CDN 域名**（腾讯云 CDN 源站为 COS 时不加此头）。

### 4.1 按证据强度排序的处理顺序

**调整 ①（最可能，先做）：改用境外网络提交**
中文播客圈普遍反映 Apple Podcasts Connect 后台**在中国大陆访问很不稳定**（官方运营攻略明确写过「管理后台在中国大陆地区的可访问性较差，打开速度会比较慢」）。浏览器 → Apple 接口的调用超时，就会抛出这个通用错误——而它与 feed 无关。
- 开**全局模式**代理（不是 PAC/规则模式），用 **Chrome 无痕窗口**，关掉广告拦截插件。
- 再进 Podcasts Connect 重试 Add。

**调整 ②：先看后台有没有「半成品」节目**
你昨天已经提交过一次。Apple 有可能已经建了一条记录，此时**再 Add 同一个 feed 会失败**。
- 登录后先看 **Shows 列表**：若已有 `AI答不锂`，**不要再 Add**，直接点进该节目 → 用 **Refresh / 刷新** 让它重新抓取。
- 若列表是空的，再走 Add 流程。

**调整 ③：确认 Apple ID 商业条款已初始化**
- 手机：设置 → 顶部 Apple ID → **付款与配送**，确认有付款方式（播客免费也必须有）。
- 在 **App Store 下载任意一个免费 App**——这一步会强制 Apple ID 完成商业条款初始化，之后再回 Podcasts Connect。
- Apple 官方社区中，这个通用报错绝大多数发生在**接受服务条款**这一步失败，而非 feed 问题。

**调整 ④（根治托管异常，免费 3 分钟）：给 COS 挂一层 Cloudflare 边缘代理**
上面 4.0 的 `attachment` 头是**客观存在**的合规风险，且中国大陆来源的 COS 对 Apple 美国抓取节点也不够友好。用一个免费 Cloudflare Worker 在边缘把这两个头摘掉，同时让 Apple 从更近的边缘取数据：
1. 注册 https://dash.cloudflare.com （免费，无需域名、无需备案）。
2. 左侧 **Workers & Pages** → **Create** → **Worker** → 起个名字（如 `aidabuli`）→ Deploy。
3. 点 **Edit code**，把 `D:\Projects\energy-daily\tools\cloudflare-worker.js` 的**全部内容**粘进去覆盖，**Deploy**。
4. 你会得到一个地址，形如：`https://aidabuli.<你的账号>.workers.dev`
5. 验证（把域名换成你的）：
   ```
   curl -s -D - -o /dev/null "https://aidabuli.<你的账号>.workers.dev/energy-daily/podcast.xml" | grep -i "content-disposition"
   ```
   **应当什么都搜不到**（头已被摘掉）。
6. 验证通过后告诉我这个地址，我把 `energy-daily/config.json` 的 `cos.public_base` 改成它并重发 feed——**封面、音频、feed 三个链接会自动整体切过去，无需重新上传任何文件**（脚本已支持自动改写链接）。
7. 用新地址提交 Apple：
   ```
   https://aidabuli.<你的账号>.workers.dev/energy-daily/podcast.xml
   ```

> 判断依据：feed 本身已用 W3C Feed Validator（境外服务器，等同 Apple 爬虫视角）验证为 **valid RSS**，所有 URL 均 200，HEAD 与 byte-range（206）均正常。所以问题不在 feed 内容，而在「账号/网络」或「托管响应头」。

### 4.2 如果以上都不行

- 联系 **Podcasts Support**：https://podcastsconnect.apple.com/contact-us （不要找普通 Apple 支持，他们不管播客）。
- 带上这条信息：`Feed validates as valid RSS (W3C). All assets return HTTP 200, HEAD + byte-range supported. Request returns generic UI error.`


---

## 五、feed 当前状态快照（2026-10-05 21:15 实测）

| 项 | 值 | 校验 |
| --- | --- | --- |
| 节目名 | AI答不锂 | ✅ |
| 主播/作者 | 老锂 | ✅ |
| Owner 邮箱 | aidabuli@agent.qq.com | ✅ |
| 语言 | zh-cn | ✅ |
| 分类 | Business › Investing | ✅ |
| explicit | false（频道 + 单集均已合法化） | ✅ |
| 封面 | https://aili-1500638180.cos.ap-shanghai.myqcloud.com/energy-daily/cover.jpg | 200 / image/jpeg / 324,998 B |
| 音频 | .../energy-daily/audio/energydaily-2026-10-05.mp3 | 200 / audio/mpeg / 2,622,327 B |
| Byte-range | Range: bytes=0-2047 | 206 ✅（Apple 流式播放必需） |
| 官网 | https://aili-1500638180.cos-website.ap-shanghai.myqcloud.com/ | 200 / text/html |
| RSS 抓取 | 同上 | 200 / application/rss+xml |

**每日门锁**：别去手改 COS 上的 `podcast.xml`。要改元数据就改 `energy-daily/config.json`，否则次日 07:25 自动重生成会把你的改动覆盖掉。

/**
 * AI答不锂 · RSS/音频 边缘代理 (Cloudflare Worker)
 * ------------------------------------------------------------------
 * 为什么需要它:
 *   腾讯云 COS 规定 —— 2024-01-01 之后创建的存储桶, 用「默认域名 / 静态网站域名 /
 *   全球加速域名」访问任何对象, 都会强制追加两个响应头:
 *       Content-Disposition: attachment
 *       x-cos-force-download: true
 *   并且对象级设置 Content-Disposition: inline 也无法覆盖 (官方文档: 强制下载优先级更高)。
 *   官方给的解法只有两个: 绑自定义域名(需备案), 或走 CDN 域名。
 *   本 Worker 就是「免备案、免域名、免费」的第三条路: 在 Cloudflare 全球边缘把这两个头摘掉,
 *   顺带让 Apple 的抓取节点从更近的边缘取数据。
 *
 * 部署后得到形如 https://<你的名字>.<账号>.workers.dev 的地址, 用法:
 *   https://xxx.workers.dev/energy-daily/podcast.xml
 *   https://xxx.workers.dev/energy-daily/cover.jpg
 *   https://xxx.workers.dev/energy-daily/audio/energydaily-2026-10-05.mp3
 * 即: 把原来 COS 域名整段替换成 Worker 域名即可, 路径保持不变。
 *
 * 部署完成后, 把 energy-daily/config.json 里的 cos.public_base 改成
 *   "https://xxx.workers.dev"
 * 再跑一次 scripts/publish.py, feed 里的封面/音频链接会自动切到新地址。
 */

const ORIGIN = "https://aili-1500638180.cos.ap-shanghai.myqcloud.com";

/** 需要摘除的响应头(小写) */
const STRIP = ["content-disposition", "x-cos-force-download", "x-cos-request-id"];

export default {
  async fetch(request) {
    const url = new URL(request.url);

    // 只代理 energy-daily/ 前缀下的资源, 其余给个明确提示
    if (!url.pathname.startsWith("/energy-daily/")) {
      return new Response(
        "AI答不锂 · 边缘代理\n\n请访问 /energy-daily/podcast.xml 获取播客订阅源。\n",
        { status: 404, headers: { "content-type": "text/plain; charset=utf-8" } }
      );
    }

    // 原样转发(包含 Range 头, 保证音频可拖动播放/流式下载; Apple 要求 byte-range)
    const upstream = await fetch(new Request(ORIGIN + url.pathname + url.search, request));

    const headers = new Headers(upstream.headers);
    for (const h of STRIP) headers.delete(h);

    // 缓存策略: feed 短缓存(便于每日更新), 音视频/图片长缓存(文件名带日期, 内容不变)
    const isFeed = url.pathname.endsWith(".xml");
    headers.set(
      "cache-control",
      isFeed ? "public, max-age=300" : "public, max-age=31536000, immutable"
    );

    // HEAD 请求不能带 body
    const body = request.method === "HEAD" ? null : upstream.body;
    return new Response(body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers,
    });
  },
};

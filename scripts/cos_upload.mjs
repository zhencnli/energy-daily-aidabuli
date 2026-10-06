#!/usr/bin/env node
/**
 * Energy Daily - 腾讯云 COS 上传 / 托管工具
 *
 * 依赖: cos-nodejs-sdk-v5 (已安装在 energy-daily/node_modules)
 * 凭证: 优先读环境变量, 其次读同目录上一级 .env
 *   TENCENT_COS_SECRET_ID / TENCENT_COS_SECRET_KEY / TENCENT_COS_REGION / TENCENT_COS_BUCKET
 *
 * 用法:
 *   node cos_upload.mjs discover                              # 列出账号下的存储桶
 *   node cos_upload.mjs create-bucket --name <b> --region <r> # 创建公开读存储桶并回填 .env
 *   node cos_upload.mjs upload --file <path> --key <key> [--content-type <ct>] [--private] [--cache-control <cc>]
 *   node cos_upload.mjs sign --key <key> [--expires 3600]     # 生成临时签名 URL
 *   node cos_upload.mjs exists --key <key>
 *   node cos_upload.mjs set-bucket-acl [--acl public-read]
 *
 * 所有成功输出均为一行 JSON 到 stdout, 便于上游解析。
 */
import { createRequire } from "module";
import { readFileSync, writeFileSync, existsSync, statSync, createReadStream } from "fs";
import { dirname, resolve, extname } from "path";
import { fileURLToPath } from "url";

const require = createRequire(import.meta.url);
const COS = require("cos-nodejs-sdk-v5");

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, "..");
const ENV_PATH = resolve(ROOT, ".env");

function loadEnv() {
  if (!existsSync(ENV_PATH)) return;
  for (const line of readFileSync(ENV_PATH, "utf-8").split("\n")) {
    const t = line.trim();
    if (!t || t.startsWith("#")) continue;
    const i = t.indexOf("=");
    if (i === -1) continue;
    const k = t.slice(0, i).trim();
    let v = t.slice(i + 1).trim();
    if ((v.startsWith("'") && v.endsWith("'")) || (v.startsWith('"') && v.endsWith('"'))) v = v.slice(1, -1);
    if (!process.env[k]) process.env[k] = v;
  }
}
loadEnv();

const SecretId = process.env.TENCENT_COS_SECRET_ID;
const SecretKey = process.env.TENCENT_COS_SECRET_KEY;
let Region = process.env.TENCENT_COS_REGION;
let Bucket = process.env.TENCENT_COS_BUCKET;

if (!SecretId || !SecretKey) {
  console.log(JSON.stringify({ ok: false, error: "缺少 TENCENT_COS_SECRET_ID / SECRET_KEY" }));
  process.exit(2);
}

const cos = new COS({ SecretId, SecretKey });

const ARGS = process.argv.slice(3);
function opt(name, def = undefined) {
  const i = ARGS.indexOf(`--${name}`);
  if (i === -1) return def;
  const v = ARGS[i + 1];
  if (!v || v.startsWith("--")) return true;
  return v;
}
const has = (name) => ARGS.includes(`--${name}`);

const MIME = {
  ".wav": "audio/wav",
  ".mp3": "audio/mpeg",
  ".xml": "application/rss+xml; charset=utf-8",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".png": "image/png",
  ".json": "application/json; charset=utf-8",
  ".md": "text/markdown; charset=utf-8",
};

function publicUrl(key) {
  return `https://${Bucket}.cos.${Region}.myqcloud.com/${key}`;
}

function persistEnv(patch) {
  let content = existsSync(ENV_PATH) ? readFileSync(ENV_PATH, "utf-8") : "";
  for (const [k, v] of Object.entries(patch)) {
    const re = new RegExp(`^${k}=.*$`, "m");
    const line = `${k}='${v}'`;
    if (re.test(content)) content = content.replace(re, line);
    else content += `\n${line}`;
  }
  writeFileSync(ENV_PATH, content, "utf-8");
}

async function main() {
  const action = process.argv[2];

  if (action === "discover") {
    const data = await cos.getService({});
    const buckets = (data.Buckets || []).map((b) => ({ name: b.Name, region: b.Location }));
    console.log(JSON.stringify({ ok: true, action, count: buckets.length, buckets }, null, 2));
    return;
  }

  if (action === "create-bucket") {
    const name = opt("name");
    const region = opt("region");
    if (!name || !region) throw new Error("create-bucket 需要 --name 与 --region");
    await cos.putBucket({ Bucket: name, Region: region });
    await cos.putBucketAcl({ Bucket: name, Region: region, ACL: "public-read" });
    persistEnv({ TENCENT_COS_REGION: region, TENCENT_COS_BUCKET: name });
    console.log(JSON.stringify({ ok: true, action, bucket: name, region, acl: "public-read", url: `https://${name}.cos.${region}.myqcloud.com/` }, null, 2));
    return;
  }

  if (action === "set-bucket-acl") {
    const acl = opt("acl", "public-read");
    await cos.putBucketAcl({ Bucket, Region, ACL: acl });
    console.log(JSON.stringify({ ok: true, action, bucket: Bucket, acl }));
    return;
  }

  if (!Region || !Bucket) {
    console.log(JSON.stringify({ ok: false, error: "缺少 TENCENT_COS_REGION / TENCENT_COS_BUCKET，请先执行 discover 或 create-bucket" }));
    process.exit(2);
  }

  if (action === "upload") {
    const file = opt("file");
    const key = opt("key");
    if (!file || !key) throw new Error("upload 需要 --file 与 --key");
    const abs = resolve(file);
    if (!existsSync(abs)) throw new Error(`文件不存在: ${abs}`);
    const contentType = opt("content-type") || MIME[extname(abs).toLowerCase()] || "application/octet-stream";
    const headers = { "Content-Type": contentType };
    if (!has("private")) headers["x-cos-acl"] = "public-read";
    const cc = opt("cache-control");
    if (cc) headers["Cache-Control"] = cc;
    // 播客平台要求文件可内联访问; 存储桶若开了「强制下载」, 用对象级头覆盖
    const cd = opt("content-disposition", "inline");
    if (cd) headers["Content-Disposition"] = cd;
    const size = statSync(abs).size;
    await cos.putObject({
      Bucket, Region, Key: key, Body: createReadStream(abs),
      ContentLength: size, Headers: headers,
    });
    console.log(JSON.stringify({
      ok: true, action, bucket: Bucket, region: Region, key,
      size, content_type: contentType, acl: has("private") ? "private" : "public-read",
      url: publicUrl(key),
    }, null, 2));
    return;
  }

  if (action === "exists") {
    const key = opt("key");
    try {
      const data = await cos.headObject({ Bucket, Region, Key: key });
      console.log(JSON.stringify({ ok: true, action, key, exists: true, size: Number(data.headers?.["content-length"] || 0), url: publicUrl(key) }));
    } catch {
      console.log(JSON.stringify({ ok: true, action, key, exists: false }));
    }
    return;
  }

  if (action === "delete") {
    const key = opt("key");
    if (!key) throw new Error("delete 需要 --key");
    await cos.deleteObject({ Bucket, Region, Key: key });
    console.log(JSON.stringify({ ok: true, action, key, deleted: true }));
    return;
  }

  if (action === "sign") {
    const key = opt("key");
    const expires = Number(opt("expires", 3600));
    const url = cos.getObjectUrl({ Bucket, Region, Key: key, Sign: true, Expires: expires });
    console.log(JSON.stringify({ ok: true, action, key, expires, url }));
    return;
  }

  console.log(JSON.stringify({ ok: false, error: `未知 action: ${action}` }));
  process.exit(2);
}

main().catch((e) => {
  console.log(JSON.stringify({ ok: false, error: `${e.name || "Error"}: ${e.message}` }));
  process.exit(1);
});

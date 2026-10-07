#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
AI答不锂 · 制作台（Tkinter 桌面工具）

两个页面:
  [1] 语音生成  —— 上传/粘贴稿件，男声独讲 或 男女对话，调用 TTS 生成录音
  [2] 上传发布  —— 选栏目/音频(或视频)/封面，填标题简介，确认上传并发布到同一 RSS

依赖（用系统 Python 运行）:
  pip install edge-tts mutagen pillow   # tkinter 为 Python 标准库
COS 凭证: 在本仓库根目录放 .env（与 cos_upload.mjs 同格式）:
  TENCENT_COS_SECRET_ID=...
  TENCENT_COS_SECRET_KEY=...
  TENCENT_COS_REGION=ap-shanghai
  TENCENT_COS_BUCKET=aili-1500638180
阿里通义 TTS: 在环境中设置 DASHSCOPE_API_KEY（或在 .env 里加一行）

用法:
  python scripts/studio.py
"""
import json
import os
import sys
import threading
import tkinter as tk
from datetime import datetime, timedelta, timezone
from tkinter import filedialog, messagebox, scrolledtext, ttk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CST = timezone(timedelta(hours=8))

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import publish as pub  # 发布逻辑（无 tkinter 依赖）

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except Exception:
    HAS_PIL = False

CFG = {}
try:
    CFG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
except Exception:
    messagebox.showerror("配置错误", "无法读取 config.json")


# ---------------------------------------------------------------- 工具
def col_label(col):
    return CFG.get("columns", {}).get(col, {}).get("label", col)


def col_prefix(col):
    return CFG.get("columns", {}).get(col, {}).get("title_prefix", "")


def audio_duration(path):
    try:
        from mutagen import File as MF
        m = MF(path)
        if m and getattr(m, "info", None):
            return float(m.info.length)
    except Exception:
        pass
    try:
        import soundfile as sf
        info = sf.info(path)
        return info.frames / float(info.samplerate)
    except Exception:
        pass
    return 0.0


def check_cos_env():
    env = pub.load_env()
    need = ["TENCENT_COS_SECRET_ID", "TENCENT_COS_SECRET_KEY",
            "TENCENT_COS_REGION", "TENCENT_COS_BUCKET"]
    missing = [k for k in need if not env.get(k) and not os.environ.get(k)]
    return missing


# ---------------------------------------------------------------- 通用长任务
def run_long_task(fn, on_done, status_widget):
    def _wrap():
        try:
            res = fn()
        except Exception as exc:
            res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        status_widget.after(0, lambda: on_done(res))
    t = threading.Thread(target=_wrap, daemon=True)
    t.start()


# ================================================================ 主窗口
class StudioApp:
    def __init__(self, root):
        self.root = root
        self.root.title("AI答不锂 · 制作台")
        self.root.geometry("860x680")
        self.tts_out_path = None   # 最近一次生成的音频
        self.last_publish = None

        # COS 凭证检查
        miss = check_cos_env()
        if miss:
            messagebox.showwarning(
                "COS 凭证缺失",
                f"未找到腾讯云 COS 凭证: {', '.join(miss)}\n\n"
                "请在仓库根目录创建 .env（与 cos_upload.mjs 同格式）填写后重启本工具，"
                "否则上传功能不可用。生成语音不受影响。")

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_tts = ttk.Frame(self.notebook)
        self.tab_pub = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_tts, text="① 语音生成")
        self.notebook.add(self.tab_pub, text="② 上传发布")

        self._build_tts()
        self._build_pub()

    # ---------------------------------------------------- 页面一：语音生成
    def _build_tts(self):
        f = self.tab_tts
        row = 0
        ttk.Label(f, text="栏目：").grid(row=row, column=0, sticky="e", padx=4, pady=4)
        self.tts_column = ttk.Combobox(f, values=["brief", "insight"], width=14, state="readonly")
        self.tts_column.set(CFG.get("studio", {}).get("default_column", "insight"))
        self.tts_column.grid(row=row, column=1, sticky="w")

        ttk.Label(f, text="TTS 接口：").grid(row=row, column=2, sticky="e", padx=4)
        self.tts_provider = ttk.Combobox(f, values=["edge", "aliyun"], width=14, state="readonly")
        self.tts_provider.set(CFG.get("tts", {}).get("provider", "aliyun"))
        self.tts_provider.grid(row=row, column=3, sticky="w")

        row += 1
        ttk.Label(f, text="模式：").grid(row=row, column=0, sticky="e", padx=4, pady=4)
        self.tts_mode = tk.StringVar(value=CFG.get("studio", {}).get("default_mode", "dialogue"))
        ttk.Radiobutton(f, text="男声独讲（老锂）", variable=self.tts_mode, value="solo").grid(row=row, column=1, sticky="w")
        ttk.Radiobutton(f, text="男女对话（老锂+小爱）", variable=self.tts_mode, value="dialogue").grid(row=row, column=2, columnspan=2, sticky="w")

        row += 1
        ttk.Button(f, text="载入稿件文件(.txt/.md)", command=self._tts_load_file).grid(row=row, column=0, columnspan=2, sticky="w", padx=4, pady=4)
        ttk.Label(f, text="对话格式示例：\n老锂：今天聊聊…\n小爱：好的。").grid(row=row, column=2, columnspan=2, sticky="w")

        row += 1
        ttk.Label(f, text="稿件：").grid(row=row, column=0, sticky="ne", padx=4, pady=4)
        self.tts_text = scrolledtext.ScrolledText(f, height=16, wrap="word")
        self.tts_text.grid(row=row, column=1, columnspan=3, sticky="nsew", padx=4, pady=4)
        f.rowconfigure(row, weight=1)
        for c in (1, 2, 3):
            f.columnconfigure(c, weight=1)

        row += 1
        self.tts_run = ttk.Button(f, text="生成录音", command=self._tts_generate)
        self.tts_run.grid(row=row, column=0, columnspan=2, sticky="w", padx=4, pady=6)
        self.tts_pub = ttk.Button(f, text="直接发布 ▶", command=self._tts_to_publish, state="disabled")
        self.tts_pub.grid(row=row, column=2, columnspan=2, sticky="w")
        self.tts_progress = ttk.Progressbar(f, mode="indeterminate")
        self.tts_progress.grid(row=row, column=1, columnspan=3, sticky="ew", padx=4)

        row += 1
        self.tts_log = scrolledtext.ScrolledText(f, height=6, wrap="word", state="disabled")
        self.tts_log.grid(row=row, column=0, columnspan=4, sticky="nsew", padx=4, pady=4)
        f.rowconfigure(row, weight=1)

    def _tts_log_msg(self, msg):
        self.tts_log.configure(state="normal")
        self.tts_log.insert("end", msg + "\n")
        self.tts_log.configure(state="disabled")
        self.tts_log.see("end")

    def _tts_load_file(self):
        path = filedialog.askopenfilename(title="选择稿件", filetypes=[("文本", "*.txt *.md *.markdown"), ("全部", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                self.tts_text.delete("1.0", "end")
                self.tts_text.insert("1.0", fh.read())
            self._tts_log_msg(f"已载入稿件：{os.path.basename(path)}")
        except Exception as exc:
            messagebox.showerror("读取失败", str(exc))

    def _tts_generate(self):
        text = self.tts_text.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("空稿件", "请先粘贴或载入稿件文本。")
            return
        provider = self.tts_provider.get()
        mode = self.tts_mode.get()
        column = self.tts_column.get()
        cfg = CFG
        date = datetime.now(CST).strftime("%Y-%m-%d")
        out_dir = os.path.join(ROOT, cfg.get("studio", {}).get("output_dir", "studio_output"))
        os.makedirs(out_dir, exist_ok=True)
        ext = ".mp3"
        out_path = os.path.join(out_dir, f"{column}-{date}{ext}")
        cover = os.path.join(ROOT, cfg.get("studio", {}).get("cover", "assets/cover.jpg"))
        rate = cfg.get("tts", {}).get("target_chars_per_min", 260)

        self.tts_run.configure(state="disabled")
        self.tts_pub.configure(state="disabled")
        self.tts_progress.start(20)
        self._tts_log_msg(f"开始生成（provider={provider}, mode={mode}, 目标约 {rate} 字/分）…")

        def worker():
            try:
                from tts_providers import synthesize
                res = synthesize(text, mode, provider, out_path, cfg=cfg, cover=cover,
                                 progress=lambda s, i, t: self.root.after(0, lambda: self._tts_log_msg(f"合成进度 {i}/{t}")))
            except Exception as exc:
                res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self.root.after(0, lambda: self._tts_done(res))

        threading.Thread(target=worker, daemon=True).start()

    def _tts_done(self, res):
        self.tts_progress.stop()
        self.tts_run.configure(state="normal")
        if not res.get("ok"):
            self._tts_log_msg("生成失败：" + str(res.get("error")))
            messagebox.showerror("生成失败", str(res.get("error")))
            return
        self.tts_out_path = res.get("out_path")
        d = res.get("duration_sec", 0) or 0
        self._tts_log_msg(f"完成：{res.get('out_path')}")
        self._tts_log_msg(f"时长 {res.get('duration_hms')}（{d:.0f}秒），字数 {res.get('chars')}，分段 {res.get('segments')}")
        if d < 1200:
            self._tts_log_msg("⚠️ 时长不足 20 分钟，建议补充稿件。")
        elif d > 2400:
            self._tts_log_msg("⚠️ 时长超过 40 分钟，建议精简。")
        else:
            self._tts_log_msg("✅ 时长落在 20–40 分钟区间。")
        self.tts_pub.configure(state="normal")

    def _tts_to_publish(self):
        if not self.tts_out_path or not os.path.exists(self.tts_out_path):
            messagebox.showwarning("无音频", "请先生成录音。")
            return
        # 跳到上传页并预填
        self.pub_file_var.set(self.tts_out_path)
        self.pub_file_label.configure(text=os.path.basename(self.tts_out_path))
        col = self.tts_column.get()
        self.pub_column.set(col)
        self._pub_refresh_defaults()
        self.notebook.select(self.tab_pub)

    # ---------------------------------------------------- 页面二：上传发布
    def _build_pub(self):
        f = self.tab_pub
        row = 0
        ttk.Label(f, text="栏目：").grid(row=row, column=0, sticky="e", padx=4, pady=4)
        self.pub_column = ttk.Combobox(f, values=["brief", "insight"], width=14, state="readonly")
        self.pub_column.set(CFG.get("studio", {}).get("default_column", "insight"))
        self.pub_column.grid(row=row, column=1, sticky="w")
        self.pub_column.bind("<<ComboboxSelected>>", lambda e: self._pub_refresh_defaults())

        ttk.Label(f, text="日期：").grid(row=row, column=2, sticky="e", padx=4)
        self.pub_date = ttk.Entry(f, width=14)
        self.pub_date.insert(0, datetime.now(CST).strftime("%Y-%m-%d"))
        self.pub_date.grid(row=row, column=3, sticky="w")
        self.pub_date.bind("<FocusOut>", lambda e: self._pub_refresh_defaults())

        row += 1
        ttk.Label(f, text="音频/视频：").grid(row=row, column=0, sticky="e", padx=4, pady=4)
        self.pub_file_var = tk.StringVar()
        self.pub_file_label = ttk.Label(f, text="（未选择）", foreground="#666")
        self.pub_file_label.grid(row=row, column=1, columnspan=2, sticky="w")
        ttk.Button(f, text="选择文件", command=self._pub_pick_file).grid(row=row, column=3, sticky="w")

        row += 1
        ttk.Label(f, text="本集封面：").grid(row=row, column=0, sticky="e", padx=4, pady=4)
        self.pub_img_var = tk.StringVar()
        self.pub_img_label = ttk.Label(f, text="（未选择，将用频道封面）", foreground="#666")
        self.pub_img_label.grid(row=row, column=1, columnspan=2, sticky="w")
        ttk.Button(f, text="选择图片", command=self._pub_pick_img).grid(row=row, column=3, sticky="w")
        if HAS_PIL:
            self.pub_img_preview = tk.Label(f)
            self.pub_img_preview.grid(row=row, column=1, columnspan=3, sticky="w", padx=180)

        row += 1
        ttk.Label(f, text="标题：").grid(row=row, column=0, sticky="ne", padx=4, pady=4)
        self.pub_title = ttk.Entry(f, width=60)
        self.pub_title.grid(row=row, column=1, columnspan=3, sticky="ew", padx=4)

        row += 1
        ttk.Label(f, text="副标题：").grid(row=row, column=0, sticky="e", padx=4, pady=4)
        self.pub_subtitle = ttk.Entry(f, width=60)
        self.pub_subtitle.grid(row=row, column=1, columnspan=3, sticky="ew", padx=4)

        row += 1
        ttk.Label(f, text="简介/摘要：").grid(row=row, column=0, sticky="ne", padx=4, pady=4)
        self.pub_summary = scrolledtext.ScrolledText(f, height=8, wrap="word")
        self.pub_summary.grid(row=row, column=1, columnspan=3, sticky="nsew", padx=4, pady=4)
        f.rowconfigure(row, weight=1)

        row += 1
        self.pub_run = ttk.Button(f, text="确认上传并发布", command=self._pub_publish)
        self.pub_run.grid(row=row, column=0, columnspan=2, sticky="w", padx=4, pady=6)
        self.pub_progress = ttk.Progressbar(f, mode="indeterminate")
        self.pub_progress.grid(row=row, column=1, columnspan=3, sticky="ew", padx=4)

        row += 1
        self.pub_log = scrolledtext.ScrolledText(f, height=6, wrap="word", state="disabled")
        self.pub_log.grid(row=row, column=0, columnspan=4, sticky="nsew", padx=4, pady=4)
        f.rowconfigure(row, weight=1)

        self._pub_refresh_defaults()

    def _pub_refresh_defaults(self):
        col = self.pub_column.get()
        date = self.pub_date.get().strip() or datetime.now(CST).strftime("%Y-%m-%d")
        prefix = col_prefix(col)
        if not self.pub_title.get().strip():
            self.pub_title.insert(0, f"{prefix}{date}｜")
        if not self.pub_subtitle.get().strip():
            self.pub_subtitle.insert(0, col_label(col))

    def _pub_pick_file(self):
        path = filedialog.askopenfilename(title="选择音频/视频", filetypes=[
            ("音频", "*.mp3 *.wav *.m4a *.ogg"), ("视频", "*.mp4 *.mov"), ("全部", "*.*")])
        if not path:
            return
        self.pub_file_var.set(path)
        self.pub_file_label.configure(text=os.path.basename(path), foreground="#000")
        # 自动估算时长
        d = audio_duration(path)
        self._pub_log(f"已选择：{os.path.basename(path)}，估算时长 {int(d//60)}:{int(d%60):02d}")

    def _pub_pick_img(self):
        path = filedialog.askopenfilename(title="选择封面", filetypes=[("图片", "*.jpg *.jpeg *.png")])
        if not path:
            return
        self.pub_img_var.set(path)
        self.pub_img_label.configure(text=os.path.basename(path), foreground="#000")
        if HAS_PIL:
            try:
                im = Image.open(path)
                im.thumbnail((120, 120))
                self._img_tk = ImageTk.PhotoImage(im)
                self.pub_img_preview.configure(image=self._img_tk)
            except Exception:
                pass

    def _pub_log(self, msg):
        self.pub_log.configure(state="normal")
        self.pub_log.insert("end", msg + "\n")
        self.pub_log.configure(state="disabled")
        self.pub_log.see("end")

    def _pub_publish(self):
        audio = self.pub_file_var.get().strip()
        if not audio or not os.path.exists(audio):
            messagebox.showwarning("未选择文件", "请先选择音频/视频文件。")
            return
        col = self.pub_column.get()
        date = self.pub_date.get().strip()
        title = self.pub_title.get().strip()
        if not title:
            messagebox.showwarning("缺标题", "请填写标题。")
            return
        summary = self.pub_summary.get("1.0", "end").strip()
        subtitle = self.pub_subtitle.get().strip()
        img = self.pub_img_var.get().strip() or None
        duration = audio_duration(audio)

        self.pub_run.configure(state="disabled")
        self.pub_progress.start(20)
        self._pub_log(f"开始发布（栏目={col}，日期={date}，时长≈{int(duration)}秒）…")

        def worker():
            res = pub.publish_episode(
                audio_path=audio,
                title=title, summary=summary, subtitle=subtitle,
                date=date, duration=duration, column=col, image_path=img, dry_run=False,
            )
            self.root.after(0, lambda: self._pub_done(res, audio, duration))

        threading.Thread(target=worker, daemon=True).start()

    def _pub_done(self, res, audio, duration):
        self.pub_progress.stop()
        self.pub_run.configure(state="normal")
        if not res.get("ok"):
            self._pub_log("发布失败：" + str(res.get("error")))
            messagebox.showerror("发布失败", str(res.get("error")))
            return
        self.last_publish = res
        self._pub_log("✅ 发布成功！")
        self._pub_log(f"   Feed: {res.get('feed_url')}")
        self._pub_log(f"   本集: {res.get('episode_url')}")
        self._pub_log(f"   时长: {res.get('duration_hms')}（{int(duration)}秒）  共 {res.get('episode_count')} 期")
        if res.get("image_url"):
            self._pub_log(f"   封面: {res.get('image_url')}")
        messagebox.showinfo("发布成功", f"已发布到同一 RSS：\n{res.get('feed_url')}")


def main():
    root = tk.Tk()
    StudioApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

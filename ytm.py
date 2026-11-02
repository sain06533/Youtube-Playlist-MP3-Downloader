#!/usr/bin/env python3

import queue
import re
import shutil
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    import yt_dlp
    from yt_dlp.utils import DownloadCancelled
except ImportError:
    raise SystemExit("yt-dlp is not installed. Run: pip install -U yt-dlp")

LIKED_URL = "https://music.youtube.com/playlist?list=LM"
LIBRARY_FEEDS = [
    "https://www.youtube.com/feed/playlists",
    "https://www.youtube.com/feed/library",
]
SKIP_IDS = {"LL", "WL", "LM"}
BROWSERS = ["chrome", "firefox", "edge", "brave", "safari", "opera", "vivaldi", "chromium", "none (use cookies.txt)"]


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip(" .")
    return name or "untitled"


class QLogger:
    """Routes yt-dlp messages into the GUI log."""

    def __init__(self, put):
        self.put = put

    def debug(self, msg):
        if msg.startswith("[download] Destination") or msg.startswith("[ExtractAudio]"):
            self.put(("log", msg))

    def info(self, msg):
        pass

    def warning(self, msg):
        self.put(("log", f"warning: {msg}"))

    def error(self, msg):
        self.put(("log", f"ERROR: {msg}"))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("YouTube Music Downloader  (MP3 320 kbps)")
        self.geometry("760x620")
        self.minsize(640, 520)

        self.q: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None

        self.browser = tk.StringVar(value="chrome")
        self.cookies = tk.StringVar()
        self.out_dir = tk.StringVar(value=str(Path.home() / "YouTubeMusic"))
        self.do_liked = tk.BooleanVar(value=True)
        self.do_library = tk.BooleanVar(value=True)
        self.embed_art = tk.BooleanVar(value=True)

        self._build()
        self.after(100, self._poll)

        if not any(shutil.which(r) for r in ("deno", "node", "bun")):
            messagebox.showwarning(
                "JavaScript runtime not found",
                "YouTube downloads now need a JavaScript runtime, otherwise you'll get "
                "'Requested format is not available' errors.\n\n"
                "1. Install Deno:  winget install DenoLand.Deno\n"
                "2. Run:  pip install -U \"yt-dlp[default]\"\n"
                "3. Open a NEW terminal and restart this app.",
            )

        if not shutil.which("ffmpeg"):
            messagebox.showwarning(
                "ffmpeg not found",
                "ffmpeg is required to convert to MP3.\n\n"
                "Install it (winget install ffmpeg / brew install ffmpeg / apt install ffmpeg) "
                "and restart this app.",
            )

    # ---------- UI ----------
    def _build(self):
        pad = {"padx": 10, "pady": 4}
        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, padx=8, pady=8)

        # Login
        login = ttk.LabelFrame(frm, text="1. Login (needed for Liked Songs & private playlists)")
        login.pack(fill="x", **pad)
        ttk.Label(login, text="Browser you're logged in with:").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        ttk.Combobox(login, textvariable=self.browser, values=BROWSERS, state="readonly", width=24).grid(
            row=0, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(login, text="…or cookies.txt:").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(login, textvariable=self.cookies).grid(row=1, column=1, sticky="ew", padx=8, pady=4)
        ttk.Button(login, text="Browse", command=self._pick_cookies).grid(row=1, column=2, padx=8)
        login.columnconfigure(1, weight=1)

        # What to download
        what = ttk.LabelFrame(frm, text="2. What to download")
        what.pack(fill="x", **pad)
        ttk.Checkbutton(what, text="Liked Songs", variable=self.do_liked).pack(anchor="w", padx=8, pady=2)
        ttk.Checkbutton(what, text="All playlists in my library", variable=self.do_library).pack(anchor="w", padx=8, pady=2)
        ttk.Checkbutton(what, text="Embed cover art", variable=self.embed_art).pack(anchor="w", padx=8, pady=2)
        ttk.Label(what, text="Extra playlist URLs / IDs (one per line, optional):").pack(anchor="w", padx=8, pady=(6, 0))
        self.extra = tk.Text(what, height=3)
        self.extra.pack(fill="x", padx=8, pady=(0, 8))

        # Output
        out = ttk.LabelFrame(frm, text="3. Save to")
        out.pack(fill="x", **pad)
        ttk.Entry(out, textvariable=self.out_dir).pack(side="left", fill="x", expand=True, padx=8, pady=8)
        ttk.Button(out, text="Browse", command=self._pick_out).pack(side="right", padx=8)

        # Controls
        ctl = ttk.Frame(frm)
        ctl.pack(fill="x", **pad)
        self.start_btn = ttk.Button(ctl, text="Start download", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(ctl, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.status = ttk.Label(ctl, text="Idle")
        self.status.pack(side="left", padx=12)

        self.bar = ttk.Progressbar(frm, maximum=100)
        self.bar.pack(fill="x", **pad)

        # Log
        logf = ttk.Frame(frm)
        logf.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(logf, height=10, state="disabled", wrap="word")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _pick_cookies(self):
        f = filedialog.askopenfilename(title="Select cookies.txt", filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if f:
            self.cookies.set(f)

    def _pick_out(self):
        d = filedialog.askdirectory(title="Choose output folder")
        if d:
            self.out_dir.set(d)

    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------- Queue pump (thread -> UI) ----------
    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self._log(val)
                elif kind == "status":
                    self.status.config(text=val)
                elif kind == "progress":
                    self.bar["value"] = val
                elif kind == "done":
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.bar["value"] = 0
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ---------- Actions ----------
    def start(self):
        if self.worker and self.worker.is_alive():
            return
        if not shutil.which("ffmpeg"):
            messagebox.showerror("ffmpeg missing", "Install ffmpeg first, then restart the app.")
            return

        browser = self.browser.get()
        cookies = self.cookies.get().strip()
        has_login = bool(cookies) or not browser.startswith("none")
        extra = [l.strip() for l in self.extra.get("1.0", "end").splitlines() if l.strip()]

        if not extra and not has_login:
            messagebox.showinfo("Login needed", "Pick a browser or a cookies.txt file, or add playlist URLs.")
            return
        if not (self.do_liked.get() or self.do_library.get() or extra):
            messagebox.showinfo("Nothing selected", "Choose something to download.")
            return

        cfg = dict(
            browser=None if browser.startswith("none") else browser,
            cookies=cookies or None,
            out=Path(self.out_dir.get()),
            liked=self.do_liked.get() and has_login,
            library=self.do_library.get() and has_login,
            art=self.embed_art.get(),
            extra=extra,
        )
        self.stop_event.clear()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.worker = threading.Thread(target=self._run, args=(cfg,), daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        self.q.put(("status", "Stopping…"))

    # ---------- Worker thread ----------
    def _put(self, item):
        self.q.put(item)

    def _auth_opts(self, cfg):
        o = {}
        if cfg["cookies"]:
            o["cookiefile"] = cfg["cookies"]
        elif cfg["browser"]:
            o["cookiesfrombrowser"] = (cfg["browser"],)
        return o

    def _discover(self, cfg):
        opts = self._auth_opts(cfg) | {
            "quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
            "skip_download": True, "ignoreerrors": True,
        }
        found = {}
        with yt_dlp.YoutubeDL(opts) as ydl:
            for feed in LIBRARY_FEEDS:
                try:
                    info = ydl.extract_info(feed, download=False)
                except Exception as e:  # noqa: BLE001
                    self._put(("log", f"Couldn't read {feed}: {str(e)[:80]}"))
                    continue
                for entry in (info or {}).get("entries") or []:
                    if not entry:
                        continue
                    url = entry.get("url") or ""
                    m = re.search(r"list=([\w-]+)", url)
                    pid = m.group(1) if m else entry.get("id", "")
                    if pid and pid not in SKIP_IDS:
                        found[pid] = entry.get("title") or pid
                if found:
                    break
        return [(t, f"https://music.youtube.com/playlist?list={p}") for p, t in found.items()]

    def _download(self, title, url, cfg):
        out_dir = cfg["out"] / safe_name(title)
        out_dir.mkdir(parents=True, exist_ok=True)

        def hook(d):
            if self.stop_event.is_set():
                raise DownloadCancelled("stopped by user")
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    self._put(("progress", d.get("downloaded_bytes", 0) / total * 100))
                name = Path(d.get("filename", "")).stem
                self._put(("status", f"{title}: {name[:60]}"))
            elif d["status"] == "finished":
                self._put(("progress", 100))
                self._put(("status", "Converting to MP3…"))

        pps = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ]
        if cfg["art"]:
            pps.append({"key": "EmbedThumbnail"})

        opts = self._auth_opts(cfg) | {
            "format": "bestaudio/best",
            "outtmpl": str(out_dir / "%(artist,creator,uploader)s - %(title)s.%(ext)s"),
            "download_archive": str(out_dir / "archive.txt"),
            "writethumbnail": cfg["art"],
            "postprocessors": pps,
            "ignoreerrors": True,
            "retries": 5,
            "fragment_retries": 5,
            "sleep_interval": 1,
            "quiet": True,
            "no_warnings": True,
            "logger": QLogger(self._put),
            "progress_hooks": [hook],
        }
        self._put(("log", f"\n=== {title} -> {out_dir}"))
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    def _run(self, cfg):
        try:
            jobs = []
            if cfg["liked"]:
                jobs.append(("Liked Songs", LIKED_URL))
            if cfg["library"]:
                self._put(("status", "Looking up your playlists…"))
                pls = self._discover(cfg)
                self._put(("log", f"Found {len(pls)} library playlist(s)."))
                jobs.extend(pls)
            for u in cfg["extra"]:
                url = u if u.startswith("http") else f"https://music.youtube.com/playlist?list={u}"
                tag = safe_name(u.split("list=")[-1])
                jobs.append((f"playlist_{tag}", url))

            if not jobs:
                self._put(("log", "Nothing to download."))
            for i, (title, url) in enumerate(jobs, 1):
                if self.stop_event.is_set():
                    break
                self._put(("status", f"[{i}/{len(jobs)}] {title}"))
                try:
                    self._download(title, url, cfg)
                except DownloadCancelled:
                    break
                except Exception as e:  # noqa: BLE001
                    self._put(("log", f"Failed on '{title}': {e}"))

            msg = "Stopped." if self.stop_event.is_set() else "All done!"
            self._put(("log", f"\n{msg} Re-run anytime; finished tracks are skipped."))
            self._put(("status", msg))
        except Exception as e:  # noqa: BLE001
            self._put(("log", f"Fatal error: {e}"))
            self._put(("status", "Error"))
        finally:
            self._put(("done", None))


if __name__ == "__main__":
    App().mainloop()

"""喜马拉雅有声书工具 —— 图形界面。

macOS 原生窗口（tkinter/ttk），单文件可运行。

工作流：
  1. 选择套包目录（自动解析 manifest.json）
  2. 点击「登录喜马拉雅」—— 弹出真实 Chrome，用喜马拉雅 APP 扫码，登录态持久保存
  3. 填写专辑名 / 可见性等参数
  4. 点击「开始上传」—— 逐章自动上传，支持断点续传

浏览器与上传在后台线程执行，UI 通过队列回传日志与进度，保证界面不卡。
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Callable

import tkinter as tk
from tkinter import ttk

from .album import AlbumManager, list_albums
from .album_create import AlbumCreateError, AlbumDraft, create_album
from .browser import BrowserError, BrowserSession
from .landing import HOME_TITLE, PAGE_BG, LandingPage, route
from .paths import (
    data_dir,
    describe,
    ensure_dirs,
    logs_dir,
    migrate_legacy_bundle_data,
    profile_dir,
    state_dir,
)
from .package import (
    Package,
    PackageError,
    find_packages,
    latest_package,
    parse_package,
    summarize,
)
from .uploader import (
    BrowserGone,
    RiskControl,
    UploadPlan,
    UploadState,
    XimalayaUploader,
    _match_album,
)

APP_TITLE = "喜马拉雅有声书工具"


def default_package_path() -> Path:
    """默认套包目录。

    套包目录会随版本换名（…-r0003 → …-r0004），不能写死一个旧路径 ——
    旧路径被删掉之后，工具就只能对着空气报"目录不存在"。
    这里在材料目录里找最新的那一个。
    """
    found = latest_package()
    if found:
        return found
    return Path.home() / "Downloads" / "oz01a-ximalaya-zh-CN-r0003"

LOG_COLORS = {
    "info": "#1f2933",
    "ok": "#0f7b3f",
    "warn": "#b45309",
    "err": "#b91c1c",
}


class App(ttk.Frame):
    """喜马拉雅业务界面。

    做成可嵌入的页面组件：启动后先显示首页（LandingPage），点击「喜马拉雅」
    才把这一页 pack 进同一个窗口；窗口尺寸/标题统一由 main() 管理。
    """

    # 冒烟测试可置 False，避免构建页面时真的去连浏览器
    AUTO_CHECK_LOGIN = True

    def __init__(
        self,
        master: tk.Misc | None = None,
        *,
        on_home: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master)
        self._on_home = on_home

        # 应用状态
        self.package: Package | None = None
        self.session: BrowserSession | None = None
        self._last_pkg_title = ""
        self._bus: queue.Queue[tuple[str, object]] = queue.Queue()
        self._busy = False
        self._abort = threading.Event()
        self._log_file: Path | None = None
        self._log_fp = None

        # 运行期数据目录（登录态 / 断点状态 / 日志）——放在 bundle 之外，
        # 重新打包或挪动 .app 都不会把登录态弄丢
        try:
            ensure_dirs()
            moved = migrate_legacy_bundle_data()
        except Exception:  # noqa: BLE001
            moved = ""

        self._build_style()
        self._build_ui()
        self.after(100, self._drain_bus)

        self._set_package_path(default_package_path())
        # 启动就把最新套包解析好并显示出来 —— 用户一打开就能看到
        # "这套包认出来了没有"，不用先点一次「解析」
        self.after(300, self._auto_load_package)
        # 启动后自动检测登录态：用户一打开就知道能不能传，不用手动点
        self.after(600, self._auto_check_login)
        self._log(describe())
        if moved:
            self._log(f"已将旧位置的运行期数据（{moved}）搬到 {data_dir()}")

    def _auto_load_package(self) -> None:
        """启动时自动解析默认套包。

        套包换了版本之后目录名也换了，用户常常就是"打开工具 → 解析 → 上传"，
        这一步自动做掉最省事；解析失败也只是在日志里提示，不打断界面。
        """
        raw = self.path_var.get().strip()
        if not raw or not Path(raw).is_dir():
            self._log(f"⚠ 默认套包目录不存在：{raw}", "warn")
            self._log("    请点「浏览…」选择一个含 manifest.json 的套包目录", "warn")
            return
        self._load_package()

    def _auto_check_login(self) -> None:
        """启动时静默检测登录态（不弹窗、不打断）。"""
        if not self.AUTO_CHECK_LOGIN:
            return
        if self._busy:
            return
        self._busy = True
        self.login_btn.configure(state="disabled")
        self._set_status("● 检查登录态", "#b45309")
        threading.Thread(target=self._auto_login_worker, daemon=True).start()

    def _auto_login_worker(self) -> None:
        try:
            sess = BrowserSession(
                profile_dir(),
                headless=True,
                log=lambda m: self._log(m),
            )
            sess.launch()
            state = sess.check_login()
            if state.logged_in:
                self._log(f"✅ 检测到已有登录态：{state.detail}", "ok")
                self.session = sess
                self._post("login_ok", state.user or {})
                return
            # 未登录：关掉无头实例，等用户点按钮再开有头窗口
            sess.close()
            self._log("ℹ️ 尚未登录，点「登录喜马拉雅」用 APP 扫码即可", "info")
            self._post("login_need_scan", None)
        except BrowserError as exc:
            self._log(f"⚠ 登录态检测未完成：{exc}", "warn")
            self._post("login_need_scan", None)
        except Exception as exc:  # noqa: BLE001
            self._log(f"⚠ 登录态检测异常：{exc}", "warn")
            self._post("login_need_scan", None)

    # ------------------------------------------------------------------ 样式

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("aqua")  # macOS 原生主题
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Helvetica Neue", 15, "bold"))
        style.configure("Sub.TLabel", font=("Helvetica Neue", 11), foreground="#5b6472")
        style.configure("H.TLabel", font=("Helvetica Neue", 12, "bold"))
        style.configure("Card.TFrame", relief="groove", borderwidth=1)
        style.configure("Status.TLabel", font=("Helvetica Neue", 11, "bold"))
        style.configure("Warn.TLabel", font=("Helvetica Neue", 11), foreground="#b45309")

    # ------------------------------------------------------------------ 布局

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=(16, 14, 16, 10))
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        # ---- 顶部标题
        head = ttk.Frame(root)
        head.grid(row=0, column=0, sticky="ew")
        if self._on_home:
            ttk.Button(head, text="← 首页", command=self._go_home, width=8).pack(
                side="left", padx=(0, 12)
            )
        ttk.Label(head, text=APP_TITLE, style="Title.TLabel").pack(side="left")
        self.status_label = ttk.Label(
            head, text="● 就绪", style="Status.TLabel", foreground="#0f7b3f"
        )
        self.status_label.pack(side="right")

        # ---- 步骤 1：套包
        pkg_box = ttk.LabelFrame(root, text=" 1 · 选择套包 ", padding=12)
        pkg_box.grid(row=1, column=0, sticky="ew", pady=(12, 10))
        pkg_box.columnconfigure(1, weight=1)

        ttk.Label(pkg_box, text="套包目录").grid(row=0, column=0, sticky="w")
        self.path_var = tk.StringVar()
        ttk.Entry(pkg_box, textvariable=self.path_var).grid(
            row=0, column=1, sticky="ew", padx=8
        )
        ttk.Button(pkg_box, text="浏览…", command=self._choose_dir).grid(row=0, column=2)
        ttk.Button(pkg_box, text="解析", command=self._load_package).grid(
            row=0, column=3, padx=(8, 0)
        )

        self.pkg_info = ttk.Label(pkg_box, text="尚未加载套包", style="Sub.TLabel")
        self.pkg_info.grid(row=1, column=0, columnspan=4, sticky="w", pady=(10, 0))

        # ---- 中部：左章节表 / 右参数
        mid = ttk.Frame(root)
        mid.grid(row=2, column=0, sticky="nsew")
        mid.columnconfigure(0, weight=3)
        mid.columnconfigure(1, weight=2)
        mid.rowconfigure(0, weight=1)

        # 章节列表
        left = ttk.LabelFrame(mid, text=" 章节 ", padding=8)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        cols = ("no", "id", "title", "dur", "size", "sub", "img", "state")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", height=12)
        heads = {
            "no": "#",
            "id": "章节ID",
            "title": "标题",
            "dur": "时长",
            "size": "大小",
            "sub": "字幕",
            "img": "插图",
            "state": "状态",
        }
        widths = {"no": 44, "id": 62, "title": 210, "dur": 62, "size": 76, "sub": 52, "img": 52, "state": 78}
        for c in cols:
            self.tree.heading(c, text=heads[c])
            self.tree.column(c, width=widths[c], anchor="w" if c == "title" else "center")
        self.tree.column("title", stretch=True)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.tag_configure("pending", foreground="#7a8494")
        self.tree.tag_configure("ok", foreground="#0f7b3f")
        self.tree.tag_configure("err", foreground="#b91c1c")

        # 右侧参数
        right = ttk.LabelFrame(mid, text=" 2 · 登录 & 上传参数 ", padding=12)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)

        self.login_status = tk.StringVar(value="未登录")
        self.login_btn = ttk.Button(
            right, text="登录喜马拉雅", command=self._do_login
        )
        self.login_btn.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(right, textvariable=self.login_status, style="Sub.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 12)
        )

        ttk.Label(right, text="专辑名").grid(row=2, column=0, sticky="w", pady=5)
        self.album_var = tk.StringVar()
        self.album_box = ttk.Entry(right, textvariable=self.album_var)
        self.album_box.grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=5)

        album_btns = ttk.Frame(right)
        album_btns.grid(row=2, column=2, sticky="e", padx=(6, 0))
        self.pick_album_btn = ttk.Button(
            album_btns, text="选择…", command=self._do_pick_album, width=7
        )
        self.pick_album_btn.pack(side="left")
        self.check_album_btn = ttk.Button(
            album_btns, text="检查", command=self._do_check_album, width=6
        )
        self.check_album_btn.pack(side="left", padx=(4, 0))
        self.create_album_btn = ttk.Button(
            album_btns, text="新建", command=self._do_create_album, width=6
        )
        self.create_album_btn.pack(side="left", padx=(4, 0))

        ttk.Label(right, text="可见性").grid(row=3, column=0, sticky="w", pady=5)
        self.vis_var = tk.StringVar(value="private（私密仅自己可见）")
        vis = ttk.Combobox(
            right,
            textvariable=self.vis_var,
            values=[
                "private（私密仅自己可见）",
                "public（公开所有人可见）",
                "fans（仅好友可见）",
            ],
            state="readonly",
            width=24,
        )
        vis.grid(row=3, column=1, sticky="ew", padx=(8, 0), pady=5)
        vis.bind("<<ComboboxSelected>>", self._on_vis_change)
        self._vis_code = "private"

        ttk.Label(right, text="字幕轨").grid(row=4, column=0, sticky="w", pady=5)
        self.sub_var = tk.StringVar(value="lrc（双语合并）")
        sub = ttk.Combobox(
            right,
            textvariable=self.sub_var,
            values=["none（不上传）", "lrc（双语合并）", "srt（双语合并）"],
            state="readonly",
            width=24,
        )
        sub.grid(row=4, column=1, sticky="ew", padx=(8, 0), pady=5)

        ttk.Label(right, text="上传间隔").grid(row=5, column=0, sticky="w", pady=5)
        self.interval_var = tk.StringVar(value="12")
        ttk.Entry(right, textvariable=self.interval_var, width=8).grid(
            row=5, column=1, sticky="w", padx=(8, 0), pady=5
        )
        ttk.Label(right, text="秒 / 章（随机浮动）", style="Sub.TLabel").grid(
            row=5, column=2, sticky="w", padx=(6, 0)
        )

        ttk.Label(right, text="AI 合成").grid(row=6, column=0, sticky="w", pady=5)
        self.ai_var = tk.StringVar(value="yes（是，合成音频）")
        ai = ttk.Combobox(
            right,
            textvariable=self.ai_var,
            values=["yes（是，合成音频）", "no（否，真人录音）"],
            state="readonly",
            width=24,
        )
        ai.grid(row=6, column=1, sticky="ew", padx=(8, 0), pady=5)
        ttk.Label(right, text="平台「是否AI合成」", style="Sub.TLabel").grid(
            row=6, column=2, sticky="w", padx=(6, 0)
        )

        self.publish_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            right,
            text="直接发布（推荐）",
            variable=self.publish_var,
        ).grid(row=7, column=0, sticky="w", pady=(8, 4))
        self.publish_hint = ttk.Label(
            right, text="", style="Warn.TLabel", wraplength=420, justify="left"
        )
        self.publish_hint.grid(row=8, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self.publish_var.trace_add(
            "write", lambda *_: self._update_publish_hint()
        )
        self._update_publish_hint()

        self.resume_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            right, text="断点续传（跳过已发布成功的章节）", variable=self.resume_var
        ).grid(row=9, column=0, columnspan=2, sticky="w", pady=4)

        self.upload_btn = ttk.Button(
            right, text="开始上传", command=self._do_upload, state="disabled"
        )
        self.upload_btn.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(12, 4))

        self.stop_btn = ttk.Button(
            right, text="停止", command=self._do_stop, state="disabled"
        )
        self.stop_btn.grid(row=11, column=0, columnspan=2, sticky="ew", pady=(0, 4))

        ttk.Label(right, text="日志", style="H.TLabel").grid(
            row=12, column=0, columnspan=2, sticky="w", pady=(10, 4)
        )
        log_frame = ttk.Frame(right)
        log_frame.grid(row=13, column=0, columnspan=2, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        right.rowconfigure(13, weight=1)

        self.log = tk.Text(
            log_frame,
            height=10,
            wrap="word",
            font=("Menlo", 10),
            background="#fbfcfd",
            foreground=LOG_COLORS["info"],
            relief="solid",
            borderwidth=1,
            state="disabled",
        )
        ls = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=ls.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        ls.grid(row=0, column=1, sticky="ns")

        self.pbar = ttk.Progressbar(root, mode="determinate", length=400)
        self.pbar.grid(row=3, column=0, sticky="w", pady=(10, 0))

    # ------------------------------------------------------------------ 辅助

    def _post(self, kind: str, payload: object) -> None:
        self._bus.put((kind, payload))

    def _log(self, msg: str, kind: str = "info") -> None:
        self._post("log", (msg, kind))

    def _set_status(self, text: str, color: str = "#0f7b3f") -> None:
        self._post("status", (text, color))

    def _on_vis_change(self, _evt: object) -> None:
        self._vis_code = self.vis_var.get().split("（")[0]

    _vis_code = "private"

    def _update_publish_hint(self) -> None:
        """提示"不直接发布"到底会发生什么。

        喜马拉雅**没有草稿箱**：「继续添加」只是把条目留在当前页面，
        下一章重新加载页面就没了。所以不勾直接发布时，每章都要人工点
        「确认发布」才算数。
        """
        if self.publish_var.get():
            self.publish_hint.configure(text="")
        else:
            self.publish_hint.configure(
                text=(
                    "⚠ 人工确认模式：每章填完单会停下等你在浏览器点「确认发布」，"
                    "不等就等于没传（喜马拉雅没有草稿箱，填完单直接翻页会全部丢失）。"
                )
            )

    # ------------------------------------------------------------------ 套包

    def _choose_dir(self) -> None:
        d = filedialog.askdirectory(initialdir=self.path_var.get() or str(Path.home()))
        if d:
            self.path_var.set(d)
            self._load_package()

    def _set_package_path(self, path: Path) -> None:
        self.path_var.set(str(path))

    def _load_package(self) -> None:
        raw = self.path_var.get().strip()
        if not raw:
            return
        try:
            pkg = parse_package(raw)
        except PackageError as exc:
            messagebox.showerror("套包解析失败", str(exc))
            self._log(f"❌ 套包解析失败：{exc}", "err")
            return
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("套包解析失败", f"{exc}\n\n{traceback.format_exc()}")
            return

        self.package = pkg
        # 专辑名只在用户还没自己选过时才用套包标题填 ——
        # 用户手动指定的专辑不能被下一次「解析」冲掉。
        current_album = self.album_var.get().strip()
        if not current_album or current_album == self._last_pkg_title:
            self.album_var.set(pkg.title)
        self._last_pkg_title = pkg.title

        summary_text = summarize(pkg).replace("\n", "　|　")
        self.pkg_info.configure(text=summary_text)
        self._log(summarize(pkg), "ok")
        self._log(f"套包版本：{pkg.package_key}　内容指纹：{pkg.fingerprint}", "info")
        # 套包 source 里记着合成模型时，把「是否AI合成」默认成「是」
        if pkg.is_ai_synthesized:
            self.ai_var.set("yes（是，合成音频）")
            model = (pkg.manifest.get("source") or {}).get("model", "")
            self._log(f"套包标注为合成音频（{model}），已默认「是否AI合成＝是」", "warn")
        if pkg.errors:
            self._log(f"⚠ 套包存在 {len(pkg.errors)} 个问题：", "warn")
            for e in pkg.errors[:10]:
                self._log(f"    {e}", "warn")

        self._refresh_tree()
        self._post("package_loaded", None)

    def _refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        if not self.package:
            return
        for ch in self.package.chapters:
            mins, secs = divmod(int(ch.duration_seconds), 60)
            self.tree.insert(
                "",
                "end",
                iid=ch.id,
                values=(
                    ch.index + 1,
                    ch.id,
                    ch.title,
                    f"{mins}:{secs:02d}",
                    f"{ch.audio_bytes / 1024 / 1024:.1f}M",
                    "✓" if ch.lrc or ch.srt else "-",
                    len(ch.images),
                    "待上传" if ch.ok else "异常",
                ),
                tags=("pending" if ch.ok else "err",),
            )

    # ------------------------------------------------------------------ 登录

    def _do_login(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.login_btn.configure(state="disabled")
        self._set_status("● 检查登录态", "#b45309")
        self._log("正在检查登录态…", "info")
        threading.Thread(target=self._login_worker, daemon=True).start()

    def _login_worker(self) -> None:
        try:
            # 先把上一个会话（启动自检用的无头实例）关掉 ——
            # 它占着 profile 和调试端口，不关的话新窗口只能"接管"它，
            # 接管的却是个看不见的无头浏览器。
            old = self.session
            if old is not None:
                try:
                    old.close()
                except Exception:  # noqa: BLE001
                    pass
                self.session = None

            sess = BrowserSession(
                profile_dir(),
                headless=False,
                log=lambda m: self._log(m),
            )
            sess.launch()
            self.session = sess

            # 先看是否已有登录态（有就直接用，不打扰用户）
            state = sess.check_login()
            if state.logged_in:
                self._log(f"✅ {state.detail}，无需重新扫码", "ok")
                self._post("login_ok", state.user or {})
                return

            # 没有才引导扫码
            self._log("未登录，请在弹出的 Chrome 窗口用喜马拉雅 APP 扫码", "warn")
            state = sess.login_if_needed(timeout=300)
            self._post("login_ok", state.user or {})
        except BrowserError as exc:
            self._log(f"❌ 登录失败：{exc}", "err")
            self._post("login_fail", str(exc))
        except Exception as exc:  # noqa: BLE001
            self._log(f"❌ 登录异常：{exc}", "err")
            self._post("login_fail", str(exc))

    # ------------------------------------------------------------------ 专辑

    def _do_pick_album(self) -> None:
        """打开可搜索的专辑选择器。"""
        if self._busy or self.session is None:
            messagebox.showwarning("尚未登录", "请先点击「登录喜马拉雅」")
            return
        self._busy = True
        self.pick_album_btn.configure(state="disabled")
        self._set_status("● 读取专辑列表", "#b45309")
        threading.Thread(target=self._pick_album_worker, daemon=True).start()

    def _pick_album_worker(self) -> None:
        assert self.session is not None
        try:
            self.session.ensure_visible()
            albums = list_albums(self.session.page)
            if albums:
                self._log(f"读到 {len(albums)} 个专辑：", "ok")
                for a in albums:
                    n = a.get("track_count")
                    self._log(f"    · {a['title']}　（{n} 集，albumId={a['album_id']}）")
            else:
                self._log("⚠ 没读到专辑列表（可能尚未登录或接口变化）", "warn")
                detail = getattr(list_albums, "last_error", "")
                if detail:
                    self._log(f"    原因：{detail}", "warn")
            self._post("albums_loaded", albums)
        except Exception as exc:  # noqa: BLE001
            self._log(f"❌ 读取专辑列表失败：{exc}", "err")
            self._post("albums_loaded", None)

    def _show_album_picker(self, albums: list[dict], preselect: str = "") -> None:
        """可搜索的专辑选择对话框。

        ttk.Combobox 没有搜索能力，用户反馈无法在列表里找到后台新建的专辑，
        所以换成带搜索框的独立对话框：输入即过滤，双击即可选中。
        """
        dlg = tk.Toplevel(self)
        dlg.title("选择专辑")
        dlg.transient(self)
        dlg.geometry("620x420")
        dlg.minsize(520, 340)

        ttk.Label(dlg, text="搜索").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 4))
        search = tk.StringVar()
        search_entry = ttk.Entry(dlg, textvariable=search)
        search_entry.grid(
            row=0, column=1, sticky="ew", padx=(6, 0), pady=(10, 4)
        )
        dlg.columnconfigure(1, weight=1)

        listbox = tk.Listbox(dlg, height=16, font=("Helvetica Neue", 12))
        listbox.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=10)
        dlg.rowconfigure(1, weight=1)

        status = ttk.Label(dlg, text="", style="Sub.TLabel")
        status.grid(row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(4, 2))

        chosen = tk.StringVar(value="")
        ttk.Label(dlg, textvariable=chosen, style="Sub.TLabel").grid(
            row=3, column=0, columnspan=2, sticky="w", padx=10
        )

        all_items = list(albums)
        shown: list[dict] = []

        def render() -> None:
            kw = search.get().strip().lower()
            shown[:] = [
                a for a in all_items
                if not kw or kw in a["title"].lower() or kw in str(a.get("album_id", ""))
            ]
            listbox.delete(0, "end")
            for a in shown:
                n = a.get("track_count")
                cnt = f"{n} 集" if n is not None else ""
                listbox.insert("end", f"{a['title']}" + (f"　（{cnt}）" if cnt else ""))
            status.configure(text=f"显示 {len(shown)} / {len(all_items)} 个专辑")

        def pick(idx: int) -> None:
            if 0 <= idx < len(shown):
                self.album_var.set(shown[idx]["title"])
                dlg.destroy()

        def on_select(_evt: object) -> None:
            # 单击只高亮，不关对话框 —— 否则没法"先筛选再挑"
            sel = listbox.curselection()
            if sel and 0 <= sel[0] < len(shown):
                chosen.set(f"选中：{shown[sel[0]]['title']}")

        def on_double(_evt: object) -> None:
            sel = listbox.curselection()
            if sel:
                pick(sel[0])

        def on_ok() -> None:
            sel = listbox.curselection()
            if sel:
                pick(sel[0])
            else:
                dlg.destroy()

        search.trace_add("write", lambda *_: render())
        listbox.bind("<<ListboxSelect>>", on_select)
        listbox.bind("<Double-Button-1>", on_double)
        listbox.bind("<Return>", lambda e: on_ok())
        dlg.bind("<Return>", lambda e: on_ok())

        btns = ttk.Frame(dlg)
        btns.grid(row=4, column=0, columnspan=2, sticky="e", padx=10, pady=8)
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right")
        ttk.Button(btns, text="确定", command=on_ok).pack(side="right", padx=(0, 8))

        render()
        # 预选：优先精确同名，否则用模糊匹配的最佳候选
        if preselect:
            exact = [i for i, a in enumerate(shown) if a["title"] == preselect]
            if exact:
                listbox.selection_set(exact[0])
                listbox.see(exact[0])
            else:
                matched, _reason, cands = _match_album(preselect, [a["title"] for a in shown])
                target = matched or (cands[0] if cands else "")
                if target:
                    idx = next((i for i, a in enumerate(shown) if a["title"] == target), None)
                    if idx is not None:
                        listbox.selection_set(idx)
                        listbox.see(idx)
        search_entry.focus_set()

    def _do_check_album(self) -> None:
        """检查目标专辑是否已存在于账号中。"""
        if self._busy or self.session is None:
            return
        album = self.album_var.get().strip()
        if not album:
            messagebox.showwarning("缺少专辑名", "请先填写专辑名称")
            return
        self._busy = True
        self.check_album_btn.configure(state="disabled")
        self._set_status("● 检查专辑", "#b45309")
        threading.Thread(target=self._check_album_worker, args=(album,), daemon=True).start()

    def _check_album_worker(self, album: str) -> None:
        assert self.session is not None
        try:
            self.session.ensure_visible()
            albums = list_albums(self.session.page)
            titles = [a["title"] for a in albums]
            matched, reason, candidates = _match_album(album, titles)
            if matched:
                if reason == "完全匹配":
                    self._log(f"✅ 专辑《{matched}》已存在，可直接上传", "ok")
                else:
                    self._log(
                        f"✅ 账号里没有同名专辑，但匹配到《{matched}》（{reason}），"
                        "上传时会选它",
                        "ok",
                    )
            elif candidates:
                self._log(f"⚠ 《{album}》同时匹配到多个专辑，无法自动判断：", "warn")
                for c in candidates:
                    self._log(f"    · {c}", "warn")
                self._log("    请点「选择…」明确指定一个", "warn")
            else:
                self._log(
                    f"⚠ 未找到与《{album}》匹配的专辑。账号现有：{titles}。"
                    "可点「选择…」挑选，或点「新建」自动创建。",
                    "warn",
                )
            self._post("album_checked", bool(matched))
        except Exception as exc:  # noqa: BLE001
            self._log(f"❌ 检查专辑失败：{exc}", "err")
            self._post("album_checked", None)

    def _do_create_album(self) -> None:
        """打开创作中心创建新专辑。"""
        if self._busy or self.session is None:
            return
        album = self.album_var.get().strip()
        if not album:
            messagebox.showwarning("缺少专辑名", "请先填写专辑名称")
            return
        self._busy = True
        self.create_album_btn.configure(state="disabled")
        self._set_status("● 创建专辑", "#b45309")
        threading.Thread(target=self._create_album_worker, args=(album,), daemon=True).start()

    def _create_album_worker(self, album: str) -> None:
        assert self.session is not None
        try:
            self.session.ensure_visible()
            # 套包里没有专辑简介字段，简介留空，发布前可在创作中心补写
            draft = AlbumDraft(name=album, intro="")
            create_album(self.session.page, draft, log=lambda m: self._log(m))
            self._post("album_created", album)
        except AlbumCreateError as exc:
            self._log(f"❌ 创建专辑失败：{exc}", "err")
            self._post("album_created", None)
        except Exception as exc:  # noqa: BLE001
            self._log(f"❌ 创建专辑异常：{exc}", "err")
            self._post("album_created", None)

    # ------------------------------------------------------------------ 上传

    def _do_upload(self) -> None:
        if self._busy or not self.package:
            return
        album = self.album_var.get().strip()
        if not album:
            messagebox.showwarning("缺少专辑名", "请填写专辑名称")
            return
        if self.session is None:
            messagebox.showwarning("尚未登录", "请先点击「登录喜马拉雅」")
            return

        try:
            interval = max(3.0, float(self.interval_var.get() or 12))
        except ValueError:
            interval = 12.0

        plan = UploadPlan(
            album_name=album,
            visibility=self._vis_code,
            description_template="",
            subtitle_mode=self.sub_var.get().split("（")[0],
            publish=self.publish_var.get(),
            interval_range=(interval * 0.6, interval * 1.6),
            ai_synthetic=self.ai_var.get().split("（")[0],
        )
        self._busy = True
        self._abort.clear()
        self.upload_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._set_status("● 上传中", "#b45309")
        threading.Thread(
            target=self._upload_worker, args=(plan,), daemon=True
        ).start()

    def _upload_worker(self, plan: UploadPlan) -> None:
        assert self.session is not None and self.package is not None

        # 上传需要看得见的窗口：用户要能看到进度、遇到风控能人工介入
        try:
            self.session.ensure_visible()
        except BrowserError as exc:
            self._log(f"❌ 无法打开浏览器窗口：{exc}", "err")
            self._post("upload_done", (-1, 0))
            return

        if not self.session.is_alive():
            self._log("❌ 浏览器连接已断开，请关掉工具后重开再传", "err")
            self._post("upload_done", (-1, 0))
            return

        state_path = (
            state_dir() / f"{plan.album_name}.json"
        )

        # ---- 开跑前自检：专辑能不能唯一确定 ----
        # 专辑选不中时平台会把节目丢进「不进专辑」，事后在创作中心很难找回来；
        # 多个专辑同时匹配时更危险 —— 可能静默传到错误的专辑。两种都要拦下。
        existing_titles: set[str] = set()
        try:
            albums = list_albums(self.session.page)
            titles = [a["title"] for a in albums]
            matched, reason, candidates = _match_album(plan.album_name, titles)
            if not titles:
                detail = getattr(list_albums, "last_error", "")
                self._log("⚠ 读不到账号专辑列表，跳过专辑自检", "warn")
                if detail:
                    self._log(f"    原因：{detail}", "warn")
            elif matched is None and candidates:
                self._log(f"❌ 专辑自检未通过：《{plan.album_name}》同时匹配多个专辑", "err")
                for c in candidates:
                    self._log(f"    · {c}", "err")
                self._log("    请点「选择…」明确指定一个专辑后再上传", "err")
                self._post("upload_done", (-1, 0))
                return
            elif matched is None:
                self._log(f"❌ 专辑自检未通过：《{plan.album_name}》匹配不到任何现有专辑", "err")
                self._log(f"    账号现有：{titles}", "err")
                self._log("    请先点「选择…」选定一个专辑，或点「新建」创建后再上传", "err")
                self._post("upload_done", (-1, 0))
                return
            elif reason == "完全匹配":
                self._log(f"✅ 专辑自检通过：{matched}", "ok")
            else:
                self._log(f"✅ 专辑自检通过：将使用《{matched}》（{reason}）", "ok")

            # 平台专辑里已经有的章节 —— 平台才是"传没传过"的权威
            album_id = next(
                (a.get("album_id") for a in albums if a["title"] == matched), None
            )
            if album_id:
                mgr = AlbumManager(self.session.page, log=lambda m: self._log(m))
                tracks = mgr.list_tracks(album_id)
                existing_titles = {t.title for t in tracks if t.title}
                if existing_titles:
                    self._log(
                        f"ℹ 目标专辑里已有 {len(existing_titles)} 个章节，"
                        "同名章节会自动跳过"
                    )
        except Exception as exc:  # noqa: BLE001
            self._log(f"⚠ 专辑自检跳过：{exc}", "warn")

        state = UploadState(album_name=plan.album_name)
        if state_path.is_file():
            try:
                state = UploadState.from_json(state_path.read_text(encoding="utf-8"))
                self._log(
                    f"载入断点状态：已有 {len(state.done)} 章记录"
                    + (f"（套包 {state.package_key}）" if state.package_key else ""),
                    "info",
                )
            except Exception:  # noqa: BLE001
                pass

        uploader = XimalayaUploader(
            self.session.page,
            log=lambda m: self._log(m),
            on_progress=lambda cid, i, n: self._post("progress", (i, n)),
            session=self.session,
        )

        def on_result(res) -> None:
            self._post("chapter_done", res)
            try:
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_text(state.to_json(), encoding="utf-8")
            except OSError:
                pass

        try:
            results = uploader.run_batch(
                self.package, plan, state=state, resume=self.resume_var.get(),
                on_result=on_result,
                existing_titles=existing_titles,
            )
            ok = sum(1 for r in results if r.ok)
            self._log(f"—— 本次完成 {ok}/{len(results)} 章 ——", "ok" if ok else "warn")
            self._post("upload_done", (ok, len(results)))
        except BrowserGone as exc:
            self._log(f"❌ {exc}", "err")
            self._log(
                "    浏览器连接断了，已中止本批（剩下的章节没有上传）。"
                "重新点「开始上传」会从断点继续。",
                "err",
            )
            self._post("upload_done", (-1, 0))
        except RiskControl as exc:
            self._log(f"⚠ {exc}", "err")
            self._post("upload_done", (-1, 0))
        except Exception as exc:  # noqa: BLE001
            self._log(f"❌ 上传异常：{exc}", "err")
            self._log(traceback.format_exc(), "err")
            self._post("upload_done", (-1, 0))

    def _do_stop(self) -> None:
        self._abort.set()
        self._log("已请求停止（当前章节完成后生效）", "warn")

    def _go_home(self) -> None:
        """业务页顶部的「← 首页」——只切页面，不销毁（保留登录态与断点状态）。"""
        if self._on_home:
            self._on_home()

    # ------------------------------------------------------------------ 事件

    def _drain_bus(self) -> None:
        try:
            while True:
                kind, payload = self._bus.get_nowait()
                if kind == "log":
                    msg, mkind = payload  # type: ignore[misc]
                    self._append_log(str(msg), str(mkind))
                elif kind == "status":
                    text, color = payload  # type: ignore[misc]
                    self.status_label.configure(text=str(text), foreground=str(color))
                elif kind == "progress":
                    i, n = payload  # type: ignore[misc]
                    self.pbar.configure(maximum=max(1, int(n)), value=int(i))
                elif kind == "package_loaded":
                    self._set_status("● 套包已就绪", "#0f7b3f")
                elif kind == "login_ok":
                    self._busy = False
                    self.login_btn.configure(state="normal", text="重新登录")
                    user = payload if isinstance(payload, dict) else {}
                    nick = user.get("nickname") or ""
                    self.login_status.set(f"已登录 ✅ {nick}".strip())
                    self.upload_btn.configure(state="normal")
                    self._set_status("● 已登录", "#0f7b3f")
                    self._append_log("登录成功，可以开始上传", "ok")
                elif kind == "login_need_scan":
                    self._busy = False
                    self.login_btn.configure(state="normal")
                    self.login_status.set("未登录")
                    self._set_status("● 待登录", "#b45309")
                elif kind == "login_fail":
                    self._busy = False
                    self.login_btn.configure(state="normal")
                    self.login_status.set("未登录")
                    self._set_status("● 就绪", "#b91c1c")
                elif kind == "album_checked":
                    self._busy = False
                    self.check_album_btn.configure(state="normal")
                    self._set_status("● 就绪", "#0f7b3f")
                elif kind == "albums_loaded":
                    self._busy = False
                    self.pick_album_btn.configure(state="normal")
                    albums = payload or []
                    if albums:
                        self._show_album_picker(albums, self.album_var.get().strip())
                    self._set_status("● 就绪", "#0f7b3f")
                elif kind == "album_created":
                    self._busy = False
                    self.create_album_btn.configure(state="normal")
                    if payload:
                        self._set_status("● 专辑已创建", "#0f7b3f")
                    else:
                        self._set_status("● 就绪", "#b91c1c")
                elif kind == "chapter_done":
                    res = payload
                    tags = ("ok",) if res.ok else ("err",)
                    text = "已上传" if res.ok else "失败"
                    if self.tree.exists(res.chapter_id):
                        vals = list(self.tree.item(res.chapter_id, "values"))
                        vals[-1] = text
                        self.tree.item(res.chapter_id, values=vals, tags=tags)
                elif kind == "upload_done":
                    ok, total = payload  # type: ignore[misc]
                    self._busy = False
                    self.upload_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    if ok < 0:
                        self._set_status("● 已中止", "#b91c1c")
                    elif ok:
                        self._set_status(f"● 完成 {ok}/{total}", "#0f7b3f")
                    else:
                        self._set_status("● 全部失败", "#b91c1c")
        except queue.Empty:
            pass
        self.after(100, self._drain_bus)

    def _append_log(self, msg: str, kind: str = "info") -> None:
        self.log.configure(state="normal")
        tag = {"ok": "ok", "warn": "warn", "err": "err"}.get(kind)
        if tag:
            self.log.tag_configure(tag, foreground=LOG_COLORS[tag])
            self.log.insert("end", msg + "\n", tag)
        else:
            self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        # 同时落盘，出问题时可以把日志文件发出去排查
        try:
            if self._log_file is None:
                log_dir = logs_dir()
                log_dir.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                self._log_file = log_dir / f"upload-{stamp}.log"
                self._log_fp = self._log_file.open("a", encoding="utf-8")
                self._log_fp.write(
                    f"# 喜马拉雅有声书工具 运行日志 {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                )
                self.log.insert("end", f"（日志同时保存到 {self._log_file}）\n")
                self.log.see("end")
            self._log_fp.write(msg + "\n")
            self._log_fp.flush()
        except OSError:
            pass


class AppShell:
    """同一个 Tk 窗口里的页面编排：首页 ↔ 喜马拉雅业务页。

    切换只做 pack/pack_forget，业务页实例会被保留 —— 来回切换不会重复触发
    登录检测、也不会重建浏览器会话或丢掉已加载的套包。
    """

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.business: App | None = None
        self._current = "home"
        self.home = LandingPage(root, on_open=self.open_module)

    def current(self) -> str:
        return self._current

    def show_home(self) -> None:
        self._current = "home"
        self.root.title(HOME_TITLE)
        if self.business is not None:
            self.business.pack_forget()
        self.home.pack(fill="both", expand=True)

    def open_module(self, key: str) -> bool:
        """只有已上线模块（喜马拉雅）会真正切换页面。"""
        if route(key) != "app":
            return False  # 未开发模块由首页自己提示，这里不跳转
        self.home.pack_forget()
        if self.business is None:
            self.business = App(self.root, on_home=self.show_home)
        self._current = "app"
        self.root.title(APP_TITLE)
        self.business.pack(fill="both", expand=True)
        return True


def main(*, skip_landing: bool = False) -> None:
    """启动应用：默认先显示首页，点「喜马拉雅」才进入现有业务逻辑。"""
    root = tk.Tk()
    root.title(HOME_TITLE)
    root.geometry("1080x760")
    root.minsize(940, 660)
    root.configure(bg=PAGE_BG)

    shell = AppShell(root)
    if skip_landing:
        shell.open_module("ximalaya")
    else:
        shell.show_home()
    root.mainloop()

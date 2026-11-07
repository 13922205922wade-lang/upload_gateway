"""应用首页（Landing Page）——选择要进入的发布平台。

设计目标：启动后先看到四个大色块入口，只有「喜马拉雅」是已上线模块；
其余入口点击后在页面内明确显示「模块待开发」，不做任何跳转。

这一层只负责导航：业务界面仍是 xmly.gui.App，由 gui.main() 负责切换。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import tkinter as tk

HOME_TITLE = "内容发布工作台"
PENDING_TEXT = "模块待开发"

PAGE_BG = "#F4F6FA"
TEXT = "#1F2933"
MUTED = "#8A94A6"
SHADOW = "#DDE3EC"
BANNER_BG = "#FFF0E6"
BANNER_FG = "#B4451A"

FONT_TITLE = ("PingFang SC", 30, "bold")
FONT_SUB = ("PingFang SC", 13)
FONT_BANNER = ("PingFang SC", 13, "bold")
FONT_FOOT = ("PingFang SC", 11)
FONT_CARD = ("PingFang SC", 28, "bold")
FONT_CARD_SUB = ("PingFang SC", 12)
FONT_BADGE = ("PingFang SC", 20, "bold")
FONT_PILL = ("PingFang SC", 11, "bold")


@dataclass(frozen=True)
class Module:
    """首页上的一个平台入口。"""

    key: str
    name: str
    tagline: str
    color: str
    hover: str
    badge: str
    enabled: bool


MODULES: tuple[Module, ...] = (
    Module(
        key="ximalaya",
        name="喜马拉雅",
        tagline="有声书 · 批量上传与断点续传",
        color="#F26522",
        hover="#FF7C3E",
        badge="喜",
        enabled=True,
    ),
    Module(
        key="bilibili",
        name="B站",
        tagline="视频 · 稿件与合集发布",
        color="#FB7299",
        hover="#FF8CB0",
        badge="B",
        enabled=False,
    ),
    Module(
        key="xiaohongshu",
        name="小红书",
        tagline="图文 · 笔记与种草发布",
        color="#FF3B5C",
        hover="#FF6079",
        badge="书",
        enabled=False,
    ),
    Module(
        key="youtube",
        name="youtube",
        tagline="视频 · 长短视频分发",
        color="#252B3B",
        hover="#3A4359",
        badge="▶",
        enabled=False,
    ),
)

MODULE_BY_KEY = {m.key: m for m in MODULES}


def route(key: str) -> str:
    """模块路由：'app' 进入业务界面 / 'pending' 提示待开发 / 'unknown'。"""
    module = MODULE_BY_KEY.get(key)
    if module is None:
        return "unknown"
    return "app" if module.enabled else "pending"


def _rounded(canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int, r: int, color: str) -> None:
    """在 Canvas 上画一个纯色圆角矩形（tk 没有原生圆角控件）。"""
    canvas.create_rectangle(x1 + r, y1, x2 - r, y2, fill=color, outline=color)
    canvas.create_rectangle(x1, y1 + r, x2, y2 - r, fill=color, outline=color)
    for cx, cy in ((x1 + r, y1 + r), (x2 - r, y1 + r), (x1 + r, y2 - r), (x2 - r, y2 - r)):
        canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline=color)


class ColorCard(tk.Canvas):
    """一块可点击的大色块按钮。"""

    def __init__(self, master: tk.Misc, module: Module, on_click: Callable[[Module], None]) -> None:
        super().__init__(
            master,
            bg=PAGE_BG,
            highlightthickness=0,
            bd=0,
            takefocus=1,
            cursor="hand2",
        )
        self.module = module
        self._on_click = on_click
        self._hover = False
        self._flash = False

        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<FocusIn>", lambda _e: self._set_hover(True))
        self.bind("<FocusOut>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", self._activate)
        self.bind("<Return>", self._activate)
        self.bind("<space>", self._activate)

    def _activate(self, _event: object = None) -> None:
        self.focus_set()
        self._on_click(self.module)

    def _set_hover(self, value: bool) -> None:
        if self._hover != value:
            self._hover = value
            self._draw()

    def flash(self) -> None:
        """点击未开放模块时给一次就地反馈。"""
        self._flash = True
        self._draw()

        def restore() -> None:
            self._flash = False
            try:
                if self.winfo_exists():
                    self._draw()
            except tk.TclError:
                pass

        try:
            self.after(1400, restore)
        except tk.TclError:
            pass

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 120 or h < 120:
            return

        m = self.module
        active = self._hover or self._flash
        color = m.hover if active else m.color
        radius = 18

        # 阴影 + 主体色块
        _rounded(self, 6, 7, w - 3, h - 2, radius, SHADOW)
        _rounded(self, 3, 3, w - 8, h - 8, radius, color)

        # 右上角装饰圆（用 hover 色制造一点层次，保持在色块内部）
        dcx, dcy, dr = w - 100, 90, 70
        self.create_oval(dcx - dr, dcy - dr, dcx + dr, dcy + dr, fill=m.hover, outline=m.hover)

        # 图标
        cx, cy, r = 52, int(h * 0.29), 23
        self.create_oval(cx - r, cy - r, cx + r, cy + r, fill="#FFFFFF", outline="#FFFFFF")
        self.create_text(cx, cy, text=m.badge, fill=color, font=FONT_BADGE)

        # 标题 / 副标题
        title_y = int(h * 0.55)
        self.create_text(28, title_y, anchor="w", text=m.name, fill="#FFFFFF", font=FONT_CARD)
        sub = f"{PENDING_TEXT} · 敬请期待" if (self._flash and not m.enabled) else m.tagline
        self.create_text(
            29, title_y + 34, anchor="w", text=sub, fill="#FFFFFF", font=FONT_CARD_SUB
        )

        # 右下角状态胶囊
        pill_w = 126
        px1, py1, px2, py2 = w - 26 - pill_w, h - 54, w - 26, h - 20
        _rounded(self, px1, py1, px2, py2, 14, "#FFFFFF")
        label = "点击进入 →" if m.enabled else PENDING_TEXT
        self.create_text(
            (px1 + px2) / 2,
            (py1 + py2) / 2,
            text=label,
            fill=color,
            font=FONT_PILL,
        )


class LandingPage(tk.Frame):
    """首页：标题 + 提示横幅 + 2×2 大色块 + 页脚。"""

    def __init__(self, master: tk.Misc, on_open: Callable[[str], None]) -> None:
        super().__init__(master, bg=PAGE_BG)
        self._on_open = on_open
        self._cards: dict[str, ColorCard] = {}
        self._reset_id: str | None = None
        self._build()

    def _build(self) -> None:
        head = tk.Frame(self, bg=PAGE_BG)
        head.pack(fill="x", padx=40, pady=(34, 0))
        tk.Frame(head, bg="#F26522", width=6, height=38).pack(side="left", padx=(0, 14))
        tk.Label(
            head, text=HOME_TITLE, font=FONT_TITLE, fg=TEXT, bg=PAGE_BG, anchor="w"
        ).pack(side="left")

        tk.Label(
            self,
            text="选择一个平台开始 · 目前只有「喜马拉雅」模块已上线",
            font=FONT_SUB,
            fg=MUTED,
            bg=PAGE_BG,
            anchor="w",
        ).pack(fill="x", padx=60, pady=(8, 0))

        self.banner = tk.Label(
            self,
            text="",
            font=FONT_BANNER,
            fg=PAGE_BG,
            bg=PAGE_BG,
            anchor="w",
            padx=14,
            height=2,
        )
        self.banner.pack(fill="x", padx=40, pady=(14, 0))

        grid = tk.Frame(self, bg=PAGE_BG)
        grid.pack(fill="both", expand=True, padx=40, pady=(0, 8))
        for i in range(2):
            grid.rowconfigure(i, weight=1, uniform="row")
            grid.columnconfigure(i, weight=1, uniform="col")
        for i, m in enumerate(MODULES):
            card = ColorCard(grid, m, self._on_card_click)
            row, col = divmod(i, 2)
            card.grid(
                row=row,
                column=col,
                sticky="nsew",
                padx=(0, 10) if col == 0 else (10, 0),
                pady=8,
            )
            self._cards[m.key] = card

        tk.Label(
            self,
            text="已上线：喜马拉雅　·　其余模块开发中　|　关闭窗口即可退出",
            font=FONT_FOOT,
            fg=MUTED,
            bg=PAGE_BG,
            anchor="w",
        ).pack(fill="x", padx=40, pady=(4, 20))

    # ---------------------------------------------------------------- 交互

    def _on_card_click(self, module: Module) -> None:
        if route(module.key) == "app":
            self._reset_banner()
            self._on_open(module.key)
            return
        self._show_pending(module)

    def _show_pending(self, module: Module) -> None:
        """未开发模块：横幅 + 卡片就地反馈，不弹窗、不打断。"""
        self.banner.configure(
            text=f"「{module.name}」{PENDING_TEXT}，敬请期待",
            bg=BANNER_BG,
            fg=BANNER_FG,
        )
        card = self._cards.get(module.key)
        if card is not None:
            card.flash()
        if self._reset_id is not None:
            try:
                self.after_cancel(self._reset_id)
            except tk.TclError:
                pass
        self._reset_id = self.after(3000, self._reset_banner)

    def _reset_banner(self) -> None:
        self._reset_id = None
        self.banner.configure(text="", bg=PAGE_BG, fg=PAGE_BG)

    # 供测试 / 冒烟脚本直接触发点击
    def click(self, key: str) -> None:
        module = MODULE_BY_KEY.get(key)
        if module is not None:
            self._on_card_click(module)


__all__ = [
    "HOME_TITLE",
    "PENDING_TEXT",
    "MODULES",
    "MODULE_BY_KEY",
    "Module",
    "LandingPage",
    "ColorCard",
    "route",
]

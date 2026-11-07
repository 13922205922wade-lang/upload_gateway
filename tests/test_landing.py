"""首页导航自测（离线，不需要登录喜马拉雅）。

覆盖：四个入口的配置、只有喜马拉雅能进入业务页、其余入口显示「模块待开发」、
以及首页 ↔ 业务页切换所需的页面组件约定。

运行：python tests/test_landing.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from xmly.landing import MODULES, MODULE_BY_KEY, PENDING_TEXT, route  # noqa: E402


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    names = [m.name for m in MODULES]
    keys = [m.key for m in MODULES]
    checks.append(
        (
            "四个大色块入口齐全且顺序正确",
            names == ["喜马拉雅", "B站", "小红书", "youtube"],
            f"{names}",
        )
    )
    checks.append(
        (
            "入口 key 唯一",
            len(set(keys)) == 4 and len(MODULE_BY_KEY) == 4,
            f"{keys}",
        )
    )
    checks.append(
        (
            "只有喜马拉雅已上线",
            [m.key for m in MODULES if m.enabled] == ["ximalaya"],
            f"enabled={[m.key for m in MODULES if m.enabled]}",
        )
    )
    checks.append(
        (
            "待开发文案固定",
            PENDING_TEXT == "模块待开发",
            PENDING_TEXT,
        )
    )
    checks.append(
        (
            "喜马拉雅路由到业务页",
            route("ximalaya") == "app",
            route("ximalaya"),
        )
    )
    for key in ("bilibili", "xiaohongshu", "youtube"):
        checks.append((f"{key} 路由到待开发提示", route(key) == "pending", route(key)))
    checks.append(("未知入口不会误跳转", route("not-exist") == "unknown", route("not-exist")))

    # 业务页必须能嵌入首页所在的同一个窗口（否则切换会创建第二个 Tk 实例）
    try:
        import tkinter as tk
        from tkinter import ttk

        from xmly.gui import App

        checks.append(
            (
                "业务页是可嵌入的 Frame",
                issubclass(App, ttk.Frame) and not issubclass(App, tk.Tk),
                App.__mro__[1].__name__,
            )
        )
        checks.append(
            (
                "业务页提供返回首页入口",
                hasattr(App, "_go_home") and "on_home" in App.__init__.__code__.co_varnames,
                "on_home / _go_home",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(("业务页结构检查", False, f"导入失败：{exc}"))

    # GUI 层：直接触发卡片点击，验证提示文案与回调（不起 mainloop）
    try:
        import tkinter as tk

        from xmly.landing import LandingPage

        root = tk.Tk()
        root.withdraw()
        opened: list[str] = []
        home = LandingPage(root, on_open=opened.append)
        home.pack(fill="both", expand=True)
        root.update()

        home.click("bilibili")
        banner = home.banner.cget("text")
        checks.append(
            (
                "点击 B站显示模块待开发",
                "B站" in banner and PENDING_TEXT in banner,
                banner,
            )
        )
        checks.append(
            (
                "点击待开发卡片不跳转",
                opened == [],
                f"opened={opened}",
            )
        )
        checks.append(
            (
                "卡片有就地反馈",
                home._cards["bilibili"]._flash is True,
                f"flash={home._cards['bilibili']._flash}",
            )
        )

        home.click("ximalaya")
        checks.append(
            (
                "点击喜马拉雅进入业务页",
                opened == ["ximalaya"],
                f"opened={opened}",
            )
        )
        checks.append(
            (
                "进入业务页后提示横幅已复位",
                home.banner.cget("text") == "",
                repr(home.banner.cget("text")),
            )
        )
        root.destroy()

        # 整个窗口的页面编排：首页 ↔ 业务页
        from xmly.gui import App, AppShell

        App.AUTO_CHECK_LOGIN = False  # 别真的去连浏览器
        root2 = tk.Tk()
        root2.withdraw()
        shell = AppShell(root2)
        shell.show_home()
        root2.update()
        checks.append(("启动默认停在首页", shell.current() == "home", shell.current()))
        checks.append(
            (
                "待开发模块不会切换页面",
                shell.open_module("bilibili") is False and shell.current() == "home",
                shell.current(),
            )
        )
        switched = shell.open_module("ximalaya")
        checks.append(
            (
                "喜马拉雅切换进业务页",
                switched is True
                and shell.current() == "app"
                and isinstance(shell.business, App),
                f"switched={switched} current={shell.current()}",
            )
        )
        checks.append(
            (
                "业务页可返回首页",
                shell.business._on_home == shell.show_home,
                "on_home -> show_home",
            )
        )
        shell.business._go_home()
        checks.append(
            (
                "返回首页不销毁业务页实例",
                shell.current() == "home" and shell.business is not None,
                f"current={shell.current()} cached={shell.business is not None}",
            )
        )
        root2.destroy()
    except tk.TclError as exc:
        checks.append(("GUI 点击测试", True, f"跳过（无窗口环境：{exc}）"))

    print("\n" + "=" * 62)
    failed = 0
    for name, ok, detail in checks:
        flag = "✅" if ok else "❌"
        if not ok:
            failed += 1
        print(f"  {flag} {name:32s} {detail}")
    print("=" * 62)
    print(f"{'全部通过' if not failed else f'{failed} 项失败'}（共 {len(checks)} 项）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

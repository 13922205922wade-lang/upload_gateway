#!/usr/bin/env python3
"""喜马拉雅有声书上传工具 —— 启动入口。

用法：
    python3 run.py              # 启动图形界面
    python3 run.py --check      # 自检：解析套包 + 验证浏览器可驱动
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from xmly.browser import BrowserError  # noqa: E402
from xmly.paths import describe, ensure_dirs, migrate_legacy_bundle_data, profile_dir  # noqa: E402
from xmly.package import latest_package  # noqa: E402


def default_package() -> Path:
    """默认套包目录：材料目录里最新的那一个。

    套包会随版本换目录名（…-r0003 → …-r0004），写死路径迟早指向空气。
    """
    return latest_package() or (
        Path.home() / "Downloads" / "oz01a-ximalaya-zh-CN-r0003"
    )


def self_check(package_path: str | None) -> int:
    """自检：依赖、套包解析、浏览器可驱动性。"""
    ok = True

    print("=" * 60)
    print("喜马拉雅上传工具 · 自检")
    print("=" * 60)
    try:
        ensure_dirs()
        moved = migrate_legacy_bundle_data()
        print(describe())
        if moved:
            print(f"    （已搬迁旧位置的运行期数据：{moved}）")
    except Exception as exc:  # noqa: BLE001
        print(f"    ⚠ 运行期数据目录不可用：{exc}")

    # 依赖
    print("\n[1] 依赖")
    try:
        import playwright  # noqa: F401

        print("    ✅ playwright 已安装")
    except ImportError:
        print("    ❌ 缺少 playwright：pip install playwright")
        ok = False

    try:
        import tkinter  # noqa: F401

        print("    ✅ tkinter 可用")
    except ImportError:
        print("    ❌ 缺少 tkinter（系统自带，通常无需处理）")
        ok = False

    # 套包
    print("\n[2] 套包解析")
    root = Path(package_path).expanduser() if package_path else default_package()
    print(f"    路径：{root}")
    if root.is_dir():
        try:
            from xmly.package import parse_package, summarize

            pkg = parse_package(root)
            for line in summarize(pkg).splitlines():
                print(f"    {line}")
            if pkg.errors:
                print(f"    ⚠ {len(pkg.errors)} 个问题：")
                for e in pkg.errors[:8]:
                    print(f"      {e}")
                ok = False
            else:
                print("    ✅ 套包校验通过")
        except Exception as exc:  # noqa: BLE001
            print(f"    ❌ 解析失败：{exc}")
            ok = False
    else:
        print("    ⚠ 目录不存在（可在界面中手动选择）")

    # 浏览器
    print("\n[3] 浏览器驱动")
    try:
        from xmly.browser import BrowserSession

        with BrowserSession(profile_dir(), headless=True) as sess:
            print("    ✅ 成功启动本机 Chrome")
            state = sess.check_login()
            if state.logged_in:
                print("    ✅ 已保存登录态（无需重新扫码）")
            else:
                print("    ℹ️  尚未登录，首次使用请在界面点「登录喜马拉雅」扫码")
    except BrowserError as exc:
        msg = str(exc)
        if "被占用" in msg:
            # 这不算硬错误：用户之前开的窗口还开着而已
            print("    ⚠️  浏览器配置目录被占用（之前开的窗口还开着）")
            print("    ℹ️  关掉那个窗口即可；工具本身会尝试接管它")
            print("    ✅  不影响使用，登录态已保存")
        else:
            print(f"    ❌ 浏览器启动失败：{exc}")
            ok = False
    except Exception as exc:  # noqa: BLE001
        print(f"    ❌ 浏览器启动失败：{exc}")
        ok = False

    print("\n" + "=" * 60)
    print("自检结果：" + ("全部通过 ✅" if ok else "存在问题，请查看上面提示 ⚠"))
    print("=" * 60)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="喜马拉雅有声书上传工具")
    ap.add_argument("--check", action="store_true", help="运行自检后退出")
    ap.add_argument("--package", help="套包目录路径")
    ap.add_argument(
        "--no-landing",
        action="store_true",
        help="跳过首页，直接进入喜马拉雅模块（调试用）",
    )
    args = ap.parse_args()

    if args.check:
        return self_check(args.package)

    from xmly.gui import main as gui_main

    gui_main(skip_landing=args.no_landing)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

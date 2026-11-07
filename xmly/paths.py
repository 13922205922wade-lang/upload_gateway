"""运行期数据目录（登录态 / 断点状态 / 日志）。

为什么单独一个模块：
登录态（Chrome profile）和断点状态是**贵重数据** —— 丢了登录态要重新扫码，
丢了断点状态会重复上传。打包成 .app 之后 bundle 会被 `rm -rf` 重建，
所以运行期数据不能放在 bundle 里。

规则：
- 开发态（直接跑项目目录）：用项目下的 profiles/ state/ logs/
- 打包态（.app 里）：用 ~/Library/Application Support/喜马拉雅有声书工具/
  换机器、换目录、重新打包都不影响已保存的登录态。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_DATA_NAME = "喜马拉雅有声书工具"


def _bundle_app_dir() -> Path | None:
    """如果当前跑在 .app bundle 里，返回 bundle 内的 app 目录。"""
    p = Path(__file__).resolve().parent.parent  # .../Resources/app
    if p.parent.name == "Resources" and (p.parent.parent / "Info.plist").is_file():
        return p
    return None


def data_dir() -> Path:
    """运行期数据根目录。"""
    env = os.environ.get("XIMALAYA_HOME")
    if env:
        return Path(env).expanduser()
    if _bundle_app_dir() is None:
        # 开发态：项目目录
        return Path(__file__).resolve().parent.parent
    return Path.home() / "Library" / "Application Support" / APP_DATA_NAME


def profile_dir() -> Path:
    """Chrome profile 目录（登录态所在）。"""
    return data_dir() / "profiles" / "chrome"


def state_dir() -> Path:
    """断点状态目录。"""
    return data_dir() / "state"


def log_dir() -> Path:
    """日志目录。"""
    return data_dir() / "logs"


# 兼容别名
logs_dir = log_dir


def ensure_dirs() -> None:
    for d in (profile_dir(), state_dir(), log_dir()):
        d.mkdir(parents=True, exist_ok=True)


def migrate_legacy_bundle_data() -> str:
    """把旧版本留在 bundle 里的运行期数据搬到新目录。

    老版本的 .app 把 profiles/state/logs 放在 bundle 内，重新打包就没了。
    这里做一次性搬迁，只在目标目录为空时搬（不覆盖已有数据）。
    返回搬迁说明（没搬则为空串）。
    """
    bundle = _bundle_app_dir()
    if bundle is None:
        return ""
    notes: list[str] = []
    for name in ("profiles", "state", "logs"):
        src = bundle / name
        dst = data_dir() / name
        if not src.is_dir():
            continue
        if not any(src.iterdir()):
            continue
        if dst.exists() and any(dst.iterdir()):
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst, dirs_exist_ok=True)
            notes.append(name)
        except OSError:
            continue
    return "、".join(notes)


def describe() -> str:
    """给界面/日志看的一句话说明。"""
    where = "打包版" if _bundle_app_dir() else "开发态"
    return f"运行期数据目录：{data_dir()}（{where}）"


if __name__ == "__main__":  # 手动查看/迁移
    print(describe())
    moved = migrate_legacy_bundle_data()
    print("已搬迁：" + moved if moved else "无需搬迁")

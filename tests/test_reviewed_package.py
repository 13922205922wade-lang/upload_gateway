"""审核分类包（oz-reviewed-category-zip/v1）解析自测。

覆盖：目录形态、ZIP 形态、ZIP 缓存复用、路径穿越防护、自动发现，
以及旧交付包（ximalaya-picture-audiobook）不受影响。

运行：python tests/test_reviewed_package.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from xmly.package import (  # noqa: E402
    PackageError,
    find_packages,
    find_reviewed_packages,
    parse_bilingual_lrc,
    parse_package,
    summarize,
)

AUDIO_ROOT = (
    Path.home() / "Documents" / "code" / "ebook_media_docs" / "materials" / "audio"
)
EN_DIR = AUDIO_ROOT / "oz1" / "Oz1_A_en"
OLD_DIR = AUDIO_ROOT / "oz1" / "oz01a-ximalaya-zh-CN-r0004"


def _reviewed_manifest(files: list[dict]) -> dict:
    return {
        "schema": "oz-reviewed-category-zip/v1",
        "book": "Oz1",
        "category": "A_zh",
        "chapter_units": 2,
        "payload_files": len(files),
        "user_acceptance": {
            "statement": "审计通过。",
            "recorded_at": "2026-11-07T02:02:24+00:00",
        },
        "files": files,
    }


def _small_zip(path: Path, *, evil: bool = False) -> None:
    files = [
        {"chapter": "ch01", "archive_path": "ch01/zh.mp3", "bytes": 4, "sha256": "x"},
        {"chapter": "ch01", "archive_path": "ch01/zh.lrc", "bytes": 40},
        {"chapter": "ch02", "archive_path": "ch02/zh.mp3", "bytes": 4},
        {"chapter": "ch02", "archive_path": "ch02/zh.lrc", "bytes": 20},
    ]
    if evil:
        files.append({"chapter": "ch03", "archive_path": "../evil.mp3", "bytes": 4})
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(_reviewed_manifest(files), ensure_ascii=False))
        zf.writestr("ch01/zh.mp3", b"mp3a")
        zf.writestr("ch01/zh.lrc", "[00:00.000]第1章 龙卷风\n[00:05.000]你好\n[01:30.000]结束\n")
        zf.writestr("ch02/zh.mp3", b"mp3b")
        zf.writestr("ch02/zh.lrc", "[00:00.000]龙卷风之后\n[00:20.000]结束\n")
        zf.writestr("README.txt", "测试审核包")


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    # ZIP 解压缓存放到临时目录，别污染项目目录
    old_home = os.environ.get("XIMALAYA_HOME")
    with tempfile.TemporaryDirectory(prefix="xmly-reviewed-") as tmp:
        tmp_path = Path(tmp)
        os.environ["XIMALAYA_HOME"] = str(tmp_path / "home")
        try:
            # --- 1) 真实审核目录 -------------------------------------------
            if EN_DIR.is_dir():
                pkg = parse_package(EN_DIR)
                checks.append(("新 schema 目录可解析", len(pkg.chapters) == 24, f"{len(pkg.chapters)} 章"))
                checks.append(("审核目录无校验错误", not pkg.errors, str(pkg.errors[:2])))
                checks.append(("语言按 category 推断", pkg.language == "en-US", pkg.language))
                checks.append(("章节标题已补全", "第1章" in pkg.chapters[0].title, pkg.chapters[0].title))
                checks.append(("时长从 LRC 估算", pkg.chapters[0].duration_seconds > 300, f"{pkg.chapters[0].duration_seconds}s"))
                checks.append(("AI 合成标记从旧包继承", pkg.is_ai_synthesized is True, str(pkg.is_ai_synthesized)))
                checks.append(("无插图也不报错", all(not c.images for c in pkg.chapters), "images=0"))
                checks.append(("双语 LRC 拆出中英", bool(parse_bilingual_lrc(pkg.chapters[0].lrc)[1].cues), "en cues > 0"))
                checks.append(("package_key 独立于旧包", pkg.package_key.startswith("oz-reviewed/"), pkg.package_key))
                checks.append(("摘要标注审核格式", "审核分类包" in summarize(pkg), "summarize"))
                checks.append(("ZIP 导入时 origin 为空", pkg.origin is None, str(pkg.origin)))
            else:
                checks.append(("真实审核目录（跳过）", True, f"未找到 {EN_DIR}"))

            # --- 2) 旧交付包回归 --------------------------------------------
            if OLD_DIR.is_dir():
                old = parse_package(OLD_DIR)
                checks.append(("旧格式仍解析 24 章", len(old.chapters) == 24, f"{len(old.chapters)} 章"))
                checks.append(("旧格式插图保留", any(c.images for c in old.chapters), "images > 0"))
                checks.append(("旧格式不受归一化影响", not old.manifest.get("reviewed_package"), "reviewed=False"))
            else:
                checks.append(("旧格式回归（跳过）", True, f"未找到 {OLD_DIR}"))

            # --- 3) ZIP 导入 ------------------------------------------------
            zip_path = tmp_path / "Oz1_A_zh_test.zip"
            _small_zip(zip_path)
            zpkg = parse_package(zip_path)
            checks.append(("ZIP 可直接解析", len(zpkg.chapters) == 2, f"{len(zpkg.chapters)} 章"))
            checks.append(("ZIP 无校验错误", not zpkg.errors, str(zpkg.errors[:2])))
            checks.append(
                ("ZIP 记录了原始来源", zpkg.origin == zip_path.resolve(), str(zpkg.origin))
            )
            checks.append(("ZIP 展开到运行期缓存", "expanded_packages" in str(zpkg.root), zpkg.root.name))
            checks.append(("LRC 里的章节标题", "龙卷风" in zpkg.chapters[0].title, zpkg.chapters[0].title))
            checks.append(("短标题自动补章号", zpkg.chapters[1].title.startswith("第2章"), zpkg.chapters[1].title))
            checks.append(("时长取最后时间戳", abs(zpkg.chapters[0].duration_seconds - 90.0) < 0.1, f"{zpkg.chapters[0].duration_seconds}s"))
            audio = zpkg.chapters[0].audio
            mtime = audio.stat().st_mtime_ns
            zpkg2 = parse_package(zip_path)
            checks.append(("二次导入复用缓存", zpkg2.root == zpkg.root and audio.stat().st_mtime_ns == mtime, "root/mtime 未变"))
            checks.append(("ZIP 指纹稳定", zpkg.fingerprint == zpkg2.fingerprint, zpkg.fingerprint))

            # --- 4) 路径穿越防护 --------------------------------------------
            evil_zip = tmp_path / "evil.zip"
            _small_zip(evil_zip, evil=True)
            try:
                parse_package(evil_zip)
                checks.append(("拒绝 ../ 路径", False, "居然解析成功了"))
            except PackageError as exc:
                checks.append(("拒绝 ../ 路径", "不安全" in str(exc), str(exc)))

            # --- 5) 自动发现 ------------------------------------------------
            (tmp_path / "Oz1_A_en").mkdir()
            (tmp_path / "Oz1_A_en" / "manifest.json").write_text(
                json.dumps(_reviewed_manifest([])), encoding="utf-8"
            )
            found = find_packages([tmp_path])
            names = [p.name for p in found]
            checks.append(("自动发现 ZIP 套包", "Oz1_A_zh_test.zip" in names, str(names)))
            checks.append(("自动发现审核目录", "Oz1_A_en" in names, str(names)))
            checks.append(
                ("解压缓存不算源套包", not any("Oz1_A_zh_test-" in n for n in names), str(names))
            )
            reviewed = [p.name for p in find_reviewed_packages([tmp_path])]
            checks.append(
                ("界面提示能找到审核包", "Oz1_A_zh_test.zip" in reviewed and "Oz1_A_en" in reviewed, str(reviewed))
            )

            # --- 6) 未知 schema 仍应拒绝 ------------------------------------
            bad = tmp_path / "bad"
            bad.mkdir()
            (bad / "manifest.json").write_text('{"schema":"something-else/v9"}', encoding="utf-8")
            try:
                parse_package(bad)
                checks.append(("未知 schema 仍被拒绝", False, "居然解析成功了"))
            except PackageError as exc:
                checks.append(("未知 schema 仍被拒绝", "不支持的套包 schema" in str(exc), str(exc)))
        finally:
            if old_home is None:
                os.environ.pop("XIMALAYA_HOME", None)
            else:
                os.environ["XIMALAYA_HOME"] = old_home

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

"""批量上传（用与 GUI 完全相同的 uploader 代码路径）。

- 每章完成后立刻写 state，中断可续
- 遇到风控（验证码/操作频繁）立刻停
- 全程日志落盘，便于中途查看

用法：
    python3 run_batch_cli.py                       # 用材料目录里最新的套包
    XIMALAYA_PACKAGE=<套包目录> python3 run_batch_cli.py
    XIMALAYA_ALBUM="专辑名" python3 run_batch_cli.py
    XIMALAYA_LIMIT=2 python3 run_batch_cli.py     # 只跑前 2 章（冒烟）
"""
import sys, json, time, traceback, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xmly.album import AlbumManager, list_albums
from xmly.browser import BrowserSession
from xmly.package import Package, latest_package, parse_package
from xmly.paths import ensure_dirs, log_dir, profile_dir, state_dir
from xmly.uploader import (
    BrowserGone, RiskControl, UploadPlan, UploadState, XimalayaUploader,
    _match_album,
)

BASE = Path(__file__).resolve().parent
PKG_DIR = Path(os.environ.get("XIMALAYA_PACKAGE") or (latest_package() or ""))
STATE_PATH = state_dir() / "绿野仙踪 · 喜马拉雅图片有声书 · 普通话.json"
LOG_PATH = log_dir() / f"batch-{time.strftime('%Y%m%d-%H%M%S')}.log"
INTERVAL = 25  # 秒 / 章基准（实际取 0.6~1.6 倍抖动）

_log_fp = None


def log(msg: str, kind: str = "info") -> None:
    stamp = time.strftime("%H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    global _log_fp
    if _log_fp:
        _log_fp.write(line + "\n")
        _log_fp.flush()


def main() -> int:
    global _log_fp
    ensure_dirs()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _log_fp = LOG_PATH.open("a", encoding="utf-8")

    pkg = parse_package(PKG_DIR)
    log(f"套包：{pkg.title}　共 {len(pkg.chapters)} 章")
    log(f"版本：{pkg.package_key}　指纹：{pkg.fingerprint}")
    log(f"日志：{LOG_PATH}")

    album_name = os.environ.get("XIMALAYA_ALBUM") or pkg.title

    plan = UploadPlan(
        album_name=album_name,
        visibility="private",
        description_template="",
        subtitle_mode="lrc",
        publish=True,
        interval_range=(INTERVAL * 0.6, INTERVAL * 1.6),
        ai_synthetic="yes",
    )

    state = UploadState(album_name=plan.album_name)
    if STATE_PATH.is_file():
        try:
            state = UploadState.from_json(STATE_PATH.read_text(encoding="utf-8"))
            done = [k for k, v in state.done.items() if v.ok and v.published]
            log(f"载入断点：已发布 {len(done)} 章 {done}")
        except Exception:  # noqa: BLE001
            pass

    def save_state() -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(state.to_json(), encoding="utf-8")

    with BrowserSession(
        profile_dir(), headless=True, log=lambda m: log(f"  {m}")
    ) as sess:
        sess.launch()
        st = sess.check_login()
        if not st.logged_in:
            log(f"❌ 未登录：{st.detail}")
            return 2
        log(f"✅ 已登录：{(st.user or {}).get('nickname')}")

        up = XimalayaUploader(
            sess.page, log=lambda m: log(f"  {m}"), session=sess
        )

        # 开跑前自检：专辑必须匹配得上
        albums = list_albums(sess.page)
        titles = [a["title"] for a in albums]
        matched, reason, candidates = _match_album(plan.album_name, titles)
        if not titles:
            log(f"⚠ 读不到专辑列表：{getattr(list_albums, 'last_error', '')}")
        elif not matched and candidates:
            log(f"❌ 《{plan.album_name}》同时匹配多个专辑，需明确指定：")
            for c in candidates:
                log(f"    · {c}")
            return 4
        elif not matched:
            log(f"❌ 专辑自检未通过：《{plan.album_name}》匹配不到 {titles}")
            return 3
        log(f"✅ 专辑自检通过：{matched}（{reason}）")

        # 平台专辑里已有的章节 —— 平台才是"传没传过"的权威
        existing_titles: set[str] = set()
        album_id = next(
            (a.get("album_id") for a in albums if a["title"] == matched), None
        )
        if album_id:
            tracks = AlbumManager(sess.page, log=lambda m: log(f"  {m}")).list_tracks(
                album_id
            )
            existing_titles = {t.title for t in tracks if t.title}
            if existing_titles:
                log(f"ℹ 目标专辑里已有 {len(existing_titles)} 个章节，同名章节会自动跳过")

        def on_result(res) -> None:
            save_state()
            flag = "✅" if res.published else ("◻" if res.ok else "❌")
            log(f"{flag} {res.chapter_id}　{res.message}")

        try:
            limit = int(os.environ.get("XIMALAYA_LIMIT", "0") or 0)
            if limit:
                trimmed = Package(root=pkg.root, manifest=pkg.manifest,
                                   chapters=pkg.chapters[:limit])
                log(f"⚠ 限章模式：只跑前 {limit} 章")
                results = up.run_batch(
                    trimmed, plan, state=state, resume=True, on_result=on_result,
                    existing_titles=existing_titles,
                )
            else:
                results = up.run_batch(
                    pkg, plan, state=state, resume=True, on_result=on_result,
                    existing_titles=existing_titles,
                )
        except BrowserGone as exc:
            save_state()
            log(f"❌ 浏览器连接断开，已中止：{exc}")
            return 6
        except RiskControl as exc:
            save_state()
            log(f"⚠ 风控中止：{exc}")
            return 4
        except Exception:  # noqa: BLE001
            save_state()
            log("❌ 批次异常：\n" + traceback.format_exc())
            return 5

        ok = sum(1 for r in results if r.published)
        log(f"—— 本次发布成功 {ok}/{len(results)} 章 ——")
        save_state()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

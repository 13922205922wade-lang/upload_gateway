"""浏览器会话管理。

用 Playwright 驱动本机已安装的 Google Chrome（非 headless），以持久化 profile
保存登录态：用户只需扫码登录一次，之后每次运行都能复用。

关键点：
- `channel="chrome"` 使用系统真实 Chrome，避免下载 Chromium，也更容易通过风控。
- `launch_persistent_context` 把 cookie / localStorage 落到 profiles/ 目录，
  进程退出后登录态仍在。
- 登录态判定调用 `https://www.ximalaya.com/revision/main/getCurrentUser`：
    已登录返回 `{"ret":200,"data":{"uid":...,"nickname":...}}`，
    未登录返回 `{"ret":401,"isNeedLogin":true}`。
- profile 被占用时（用户之前开的窗口还开着）不报错，而是通过 CDP **接管**
  那个窗口，沿用同一套登录态，用户不必关窗重来。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

LOGIN_CHECK_URL = "https://www.ximalaya.com/revision/main/getCurrentUser"
STUDIO_UPLOAD_URL = "https://studio.ximalaya.com/upload"
STUDIO_HOME_URL = "https://studio.ximalaya.com/"
PASSPORT_HOST = "passport.ximalaya.com"

# 调试端口：用于在 profile 被占用时接管已开着的 Chrome 窗口
CDP_PORT = 9333
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"

# 登录页上的二维码 / 状态提示
_LOGIN_HINTS = ("扫码登录", "请使用喜马拉雅APP", "第三方账号登录")


class BrowserError(Exception):
    """浏览器操作失败。"""


@dataclass
class LoginState:
    logged_in: bool
    user: dict[str, Any] | None = None
    detail: str = ""


class BrowserSession:
    """封装 Playwright + 持久化 Chrome profile 的会话。

    用法::

        with BrowserSession(profile_dir) as sess:
            sess.launch()
            if not sess.check_login():
                sess.open_login()   # 用户扫码
                sess.wait_for_login()
    """

    def __init__(
        self,
        profile_dir: str | Path,
        *,
        headless: bool = False,
        log: Callable[[str], None] | None = None,
        slow_mo: int = 0,
    ) -> None:
        self.profile_dir = Path(profile_dir).expanduser().resolve()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.slow_mo = slow_mo
        self._log = log or (lambda msg: None)
        self._pw = None
        self._ctx = None
        self.page = None
        # 接管模式：浏览器是用户自己开的，close() 不能关它
        self._attached = False
        self._attached_headless = False
        self._browser = None

    # ---------------------------------------------------------------- 生命周期

    def _profile_locked_error(self, exc: Exception) -> BrowserError | None:
        """识别「profile 被占用」并给出可操作的提示。

        Chrome 用 SingletonLock 独占 profile 目录。若用户之前开的窗口还开着
        （或上次进程没退干净），再次用同一 profile 启动会直接失败，
        原始报错是英文且很长，对用户毫无指导意义。
        """
        text = str(exc)
        if "SingletonLock" in text or "ProcessSingleton" in text or "already in use" in text:
            return BrowserError(
                "浏览器配置目录被占用，且无法接管已开着的窗口。\n\n"
                "原因：之前打开的那个 Chrome 窗口还开着，工具的登录态保存在"
                f"同一个配置目录：\n{self.profile_dir}\n\n"
                "解决办法：\n"
                "  1. 关掉之前打开的喜马拉雅 Chrome 窗口，等 2 秒后重新点「登录喜马拉雅」\n"
                "  2. 如果那个窗口已经关掉却仍报此错，是残留锁文件，"
                "可删除该目录下的 SingletonLock / SingletonCookie / SingletonSocket 后重试"
            )
        return None

    def launch(self, *, allow_attach: bool = True) -> None:
        """启动浏览器（复用持久化 profile）。

        若 profile 已被占用（之前开的窗口还开着），不报错而是**接管**那个窗口：
        通过 CDP 连上去，继续用同一套登录态。这样用户不必关窗口重来。

        allow_attach=False 时不做接管，只在 profile 被占用时报错 ——
        用于「刚关掉自己的无头实例、马上要起一个有头实例」这种场景：
        那个占用者就是几秒前自己拉起来的 Chrome，正在退出途中，
        接管过去只会拿到一个马上要死的连接。
        """
        if self._ctx is not None:
            return
        self._pw = sync_playwright().start()
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                channel="chrome",
                headless=self.headless,
                slow_mo=self.slow_mo,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=Translate,MediaRouter",
                    "--no-first-run",
                    "--no-default-browser-check",
                    f"--remote-debugging-port={CDP_PORT}",
                ],
            )
            self._ctx.set_default_timeout(30_000)
            self._ctx.set_default_navigation_timeout(60_000)
            if self._ctx.pages:
                self.page = self._ctx.pages[0]
            else:
                self.page = self._ctx.new_page()
            # 抹掉最明显的自动化特征
            self.page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            self._log("浏览器已启动（使用本机 Chrome，登录态持久保存）")
        except PWError as exc:
            locked = self._is_profile_locked(exc)
            # 注意：close() 会把 _pw 置空，而接管需要 _pw。
            # 所以先尝试接管（此时 _pw 还在），失败再清理。
            if locked and allow_attach and self._attach_existing():
                return
            self.close()
            if locked:
                raise self._profile_locked_error(exc) from exc
            raise BrowserError(f"启动 Chrome 失败：{exc}") from exc

    def is_alive(self) -> bool:
        """会话是否还可用（连接没断、页面还能执行脚本）。

        「Playwright connection closed」这类错误只有在真正去操作页面时
        才会抛出来；上传前先探一次，能避免整批章节逐个失败。
        """
        if self._ctx is None or self.page is None:
            return False
        try:
            self.page.evaluate("() => 1")
            return True
        except Exception:  # noqa: BLE001
            return False

    def reconnect(self) -> bool:
        """连接断了以后尝试重新连上同一个浏览器窗口。

        优先用 CDP 重新接管（用户窗口通常还开着），接管不到就重新拉起一个。
        """
        if self._attached:
            # 断开旧的 CDP 连接（不能 close()，那会关掉用户的窗口）
            self._browser = None
            self._ctx = None
            self.page = None
            self._attached = False
            try:
                if self._pw is not None:
                    self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None
            # 接管需要一个新的 Playwright 驱动
            try:
                self._pw = sync_playwright().start()
            except Exception as exc:  # noqa: BLE001
                self._log(f"❌ 重新连接浏览器失败：{exc}")
                return False
            if self._attach_existing():
                self._log("已重新接管浏览器窗口")
                return True
        else:
            self.close()

        self.headless = False
        try:
            self.launch()
        except BrowserError as exc:
            self._log(f"❌ 重新连接浏览器失败：{exc}")
            return False
        return self.is_alive()

    @staticmethod
    def _is_profile_locked(exc: Exception) -> bool:
        text = str(exc)
        return (
            "SingletonLock" in text
            or "ProcessSingleton" in text
            or "already in use" in text
        )

    def _attach_existing(self) -> bool:
        """尝试通过 CDP 接管已开着的 Chrome 窗口。

        成功则返回 True —— 这样用户之前授权过的登录态可以直接用，
        不需要重新扫码，也不需要关掉那个窗口。

        接管前会验一下这个窗口**是不是还活着**：接管一个正在退出的
        Chrome 会连上、几十秒后断掉，表现为每一章都报
        "Playwright connection closed"。
        """
        try:
            browser = self._pw.chromium.connect_over_cdp(CDP_URL, timeout=8000)
        except Exception as exc:  # noqa: BLE001
            self._log(f"接管已有窗口失败：{exc}")
            return False

        try:
            contexts = browser.contexts
            if not contexts:
                return False
            ctx = contexts[0]
            pages = ctx.pages
            # 找一个喜马拉雅页面；没有就新建
            page = None
            for pg in pages:
                if "ximalaya.com" in (pg.url or ""):
                    page = pg
                    break
            if page is None:
                page = pages[0] if pages else ctx.new_page()

            # 活性探测：正在退出的浏览器这里会直接抛错
            page.evaluate("() => 1")

            self._ctx = ctx
            self.page = page
            self._browser = browser
            self._attached = True
            self._ctx.set_default_timeout(30_000)
            self._ctx.set_default_navigation_timeout(60_000)
            self._log("检测到已开着的浏览器窗口，已直接接管（沿用其登录态）")
            try:
                ua = page.evaluate("() => navigator.userAgent") or ""
                self._attached_headless = "HeadlessChrome" in ua
                if self._attached_headless:
                    self._log(
                        "⚠ 接管到的是一个**无头**浏览器窗口，上传时看不到进度；"
                        "上传开始时会自动换成可见窗口"
                    )
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception as exc:  # noqa: BLE001
            self._log(f"接管已有窗口失败：{exc}")
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
            return False

    def _kill_attached_headless(self) -> bool:
        """结束接管的**无头**浏览器进程。

        只对无头实例动手：用户自己开着的可见窗口绝不能被关掉。
        无头实例基本都是本工具上一次运行遗留的（它会一直占着 profile，
        导致新窗口起不来）。
        """
        if not self._attached or not self._attached_headless:
            return False
        try:
            cdp = self._ctx.new_cdp_session(self.page)
            cdp.send("Browser.close")
        except Exception as exc:  # noqa: BLE001
            self._log(f"结束无头实例失败（改用断开连接）：{exc}")
            return False
        finally:
            self._attached = False
            self._attached_headless = False
            self._browser = None
            self._ctx = None
            self.page = None
            try:
                if self._pw is not None:
                    self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None
        self._log("已结束遗留的无头浏览器实例")
        return True

    def close(self) -> None:
        """关闭会话。

        接管模式（_attached）下**不能**关掉浏览器 —— 那是用户自己的窗口，
        只断开 CDP 连接即可（停掉 Playwright 驱动就够了，不要调
        `browser.close()`：对 connect_over_cdp 连上来的浏览器，那会真的
        把用户的 Chrome 关掉）。

        自己启动的浏览器则必须先 `context.close()` —— 只有它会连带把 Chrome
        进程真正关掉、释放 profile 锁。之前这里写成了 `context.stop()`，
        而 BrowserContext 根本没有 stop() 方法，异常被吞掉 → **Chrome 永远
        不退出**，profile 一直被占着；紧接着的下一次启动就会失败并改成
        "接管"，于是接管到了一个正在退出的浏览器，几十秒后连接断掉，
        表现为每一章都报 "Playwright connection closed"。
        """
        if self._attached:
            # 只断开 CDP 连接，不动用户窗口
            self._browser = None
            self._ctx = None
            self.page = None
            self._attached = False
            try:
                if self._pw is not None:
                    self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None
            return

        try:
            if self._ctx is not None:
                self._ctx.close()
        except Exception:  # noqa: BLE001 - 关闭失败无需中断
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._ctx = None
        self._pw = None
        self.page = None

    def ensure_visible(self) -> None:
        """确保当前会话是**可见**窗口。

        自动检测登录态用的是无头实例；上传时用户需要看得见进度、
        遇到风控也要能人工介入，所以切到有头窗口。
        切换时登录态不变（同一 profile）。

        接管模式下（用户自己的窗口）本来就可见，但要确认连接还活着 ——
        接管的窗口可能已经被关掉了。

        切换的关键是**顺序**：先真正关掉无头 Chrome（context.close()），
        等 profile 锁真的释放，再起有头实例。抢在锁释放前启动会失败，
        然后旧逻辑会退化成"接管"，接管到一个正在退出的浏览器 ——
        表现为连上后几十秒连接就断，每一章都报 Playwright connection closed。
        """
        if self._attached:
            if self._attached_headless:
                # 接管到的是无头实例（多半是本工具上次运行留下的孤儿）：
                # 结束它，换一个真正看得见的窗口。
                self._log("接管到的窗口是无头的，正在换成可见窗口…")
                if self._kill_attached_headless():
                    self._wait_profile_free(timeout=20.0)
                    self._clear_stale_lock()
                    self.headless = False
                    self._launch_visible()
                    self._log("已切换到可见浏览器窗口")
                    return
            elif self.is_alive():
                return  # 用户自己的可见窗口，直接用
            self._log("浏览器连接已断开，正在重新接管…")
            if self.reconnect():
                return
            raise BrowserError("浏览器连接已断开，且无法重新连接，请重开工具")

        if not self.headless:
            return

        self.close()
        # 无头实例退出后，Chrome 可能残留 SingletonLock，
        # 导致紧接着的有头实例启动失败。等锁真的释放再清残留锁。
        self._wait_profile_free(timeout=15.0)
        self._clear_stale_lock()
        self.headless = False
        self._launch_visible()
        self._log("已切换到可见浏览器窗口")

    def _launch_visible(self, attempts: int = 3) -> None:
        """启动有头窗口；profile 还被占着就等一等再试，而不是去接管。"""
        last: BrowserError | None = None
        for _ in range(attempts):
            try:
                # 不接管：占用者可能是几秒前刚被我们关掉、正在退出的 Chrome
                self.launch(allow_attach=False)
                if self.is_alive():
                    return
                last = BrowserError("浏览器启动后没有响应")
            except BrowserError as exc:
                last = exc
                self._wait_profile_free(timeout=8.0)
                self._clear_stale_lock()
            time.sleep(1.0)
        raise last or BrowserError("无法启动可见浏览器窗口")

    def _lock_owner_alive(self) -> bool:
        """SingletonLock 指向的 Chrome 进程是否还活着。"""
        lock = self.profile_dir / "SingletonLock"
        if not lock.exists():
            return False
        try:
            target = os.readlink(lock)
            pid = int(target.rsplit("-", 1)[-1])
        except (ValueError, OSError):
            return False
        try:
            os.kill(pid, 0)  # 探测进程是否存活
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # 存在但无权限查看 → 视为存活

    def _wait_profile_free(self, timeout: float = 10.0) -> bool:
        """等 profile 的 Singleton 锁真正释放。

        刚被关掉的 Chrome 退出需要一两秒；这段时间里锁还在，
        这时候启动新实例必然失败。主人已经死掉的残锁直接清掉。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            lock = self.profile_dir / "SingletonLock"
            if not lock.exists():
                return True
            if not self._lock_owner_alive():
                self._remove_lock_files()
                return True
            time.sleep(0.3)
        return not (self.profile_dir / "SingletonLock").exists()

    def _remove_lock_files(self) -> None:
        """删除 Chrome 的锁文件（只删锁，不动任何真实数据）。"""
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            p = self.profile_dir / name
            try:
                if p.is_symlink() or p.exists():
                    p.unlink()
            except OSError:
                pass
        self._log("已清理残留的浏览器锁文件")

    def _clear_stale_lock(self, wait: float = 1.5) -> None:
        """清理 Chrome 残留的 Singleton 锁文件。

        Chrome 正常退出时会自己删这些文件；进程被杀或崩溃时会留下残锁，
        导致下次启动报 "profile 已被占用"。这里做一次保守清理：
        只删锁文件，不动任何真实数据。
        """
        import time

        deadline = time.time() + wait
        while time.time() < deadline:
            lock = self.profile_dir / "SingletonLock"
            if not lock.exists():
                return
            # 锁是符号链接，指向 hostname-PID
            if self._lock_owner_alive():
                time.sleep(0.3)
                continue  # 进程还活着，确实被占用，不能删

            # 锁已失效（进程已死）→ 清理
            self._remove_lock_files()
            return

    def reopen_visible(self) -> None:
        """强制重开一个有头窗口（登录后调用，确保用户能看到扫码页）。"""
        self.close()
        self._wait_profile_free(timeout=15.0)
        self._clear_stale_lock()
        self.headless = False
        self.launch()

    def __enter__(self) -> "BrowserSession":
        self.launch()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- 登录态

    def check_login(self) -> LoginState:
        """检查当前是否已登录喜马拉雅。

        接口两种返回形态都要认：
          已登录： {"ret":200,"msg":"用户基本信息","data":{"uid":...,"nickname":...}}
          未登录： {"ret":401,"msg":"need login!","isNeedLogin":true}

        注意：`isNeedLogin` 字段只在**未登录**时出现，已登录时压根没有这个键，
        所以不能只靠它判断，必须看 ret / data。
        """
        if self.page is None:
            raise BrowserError("浏览器尚未启动")
        try:
            resp = self.page.request.get(LOGIN_CHECK_URL, timeout=15_000)
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return LoginState(False, None, f"登录态检查失败：{exc}")

        if not isinstance(data, dict):
            return LoginState(False, None, "未登录")

        # 未登录的显式标记
        if data.get("isNeedLogin") is True or data.get("ret") == 401:
            return LoginState(False, None, "未登录")

        # 已登录：ret=200 且带 data
        user = data.get("data")
        if data.get("ret") == 200 and isinstance(user, dict) and user.get("uid"):
            name = user.get("nickname") or f"uid {user.get('uid')}"
            return LoginState(True, user, f"已登录：{name}")

        # 兜底：只要有 uid 就认为已登录
        if isinstance(user, dict) and user.get("uid"):
            name = user.get("nickname") or f"uid {user.get('uid')}"
            return LoginState(True, user, f"已登录：{name}")

        return LoginState(False, None, "未登录")

    def open_login(self, url: str = STUDIO_UPLOAD_URL) -> None:
        """打开需要登录的页面，触发跳转至扫码登录页。"""
        if self.page is None:
            raise BrowserError("浏览器尚未启动")
        self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        self.page.wait_for_timeout(1500)
        self._log(f"已打开：{self.page.url}")

    def wait_for_login(self, timeout: float = 300.0, poll: float = 2.0) -> LoginState:
        """等待用户完成扫码登录。

        轮询两个信号：接口返回已登录，或页面已跳离 passport 域。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.check_login()
            if state.logged_in:
                self._log("登录成功 ✅")
                return state
            if self.page and PASSPORT_HOST not in (self.page.url or ""):
                # 已跳离登录页，再确认一次接口
                time.sleep(1.0)
                state = self.check_login()
                if state.logged_in:
                    self._log("登录成功 ✅")
                    return state
            time.sleep(poll)
        raise BrowserError("等待登录超时，请确认已完成扫码")

    def login_if_needed(self, timeout: float = 300.0) -> LoginState:
        """若未登录则打开登录页等待扫码，登录后返回状态。"""
        state = self.check_login()
        if state.logged_in:
            self._log("检测到已有登录态，跳过扫码")
            return state
        self._log("未登录，请在弹出的 Chrome 窗口用喜马拉雅 APP 扫码")
        self.open_login()
        return self.wait_for_login(timeout=timeout)

    # ---------------------------------------------------------------- 工具

    def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 60_000) -> None:
        if self.page is None:
            raise BrowserError("浏览器尚未启动")
        self.page.goto(url, wait_until=wait_until, timeout=timeout)

    def wait_for_selector_safe(self, selector: str, timeout: int = 15_000):
        """等待选择器出现，超时返回 None 而不抛错。"""
        try:
            return self.page.wait_for_selector(selector, timeout=timeout)
        except (PWTimeout, PWError):
            return None

    def snapshot_dom(self, max_chars: int = 40_000) -> str:
        """抓取当前页可见结构摘要，用于诊断与适配。"""
        if self.page is None:
            return ""
        try:
            return self.page.evaluate(
                """() => {
                    const out = [];
                    const walk = (el, depth) => {
                        if (depth > 4) return;
                        for (const c of el.children) {
                            const t = (c.innerText || '').trim().slice(0, 40);
                            if (c.tagName === 'INPUT' || c.tagName === 'TEXTAREA'
                                || c.tagName === 'SELECT' || c.tagName === 'BUTTON') {
                                out.push(`${'  '.repeat(depth)}<${c.tagName.toLowerCase()} ` +
                                    `type=${c.type || ''} name=${c.name || ''} id=${c.id || ''} ` +
                                    `class="${(c.className || '').toString().slice(0,50)}" ` +
                                    `placeholder="${c.placeholder || ''}" accept="${c.accept || ''}" ` +
                                    `text="${t}">`);
                            } else if (t) {
                                out.push(`${'  '.repeat(depth)}<${c.tagName.toLowerCase()} ` +
                                    `class="${(c.className||'').toString().slice(0,50)}"> ${t}`);
                            }
                            walk(c, depth + 1);
                        }
                    };
                    walk(document.body, 0);
                    return out.join('\\n');
                }"""
            )[:max_chars]
        except Exception as exc:  # noqa: BLE001
            return f"(DOM 抓取失败：{exc})"

    def list_xhr(self, pattern: str = "") -> list[dict[str, Any]]:
        """读取浏览器性能日志中的近期 XHR（需先开启 CDP 日志）。"""
        return []

    def cookies(self) -> list[dict[str, Any]]:
        if self._ctx is None:
            return []
        return self._ctx.cookies()

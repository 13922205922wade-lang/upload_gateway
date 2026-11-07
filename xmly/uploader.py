"""上传执行层。

在已登录的浏览器会话里驱动喜马拉雅网页上传表单，逐章发布音频。

页面结构（2026-10 实测，截图核对过真实渲染结果）：
  上传页      https://www.ximalaya.com/reform-upload/page/webCenter/upload
  选择专辑    button[class*='search-select-album-btn']（下拉，点开是弹层列表）
              列表项 span[class*='album-title-text']，另有「不进专辑」「创建新专辑」
  文件入口    div.webuploader-pick（含「上传音频」文字）
  标题输入    input.ant-input[placeholder='请输入声音标题']
  简介编辑器  window.KindEditor 实例 0 + textarea#soundRichIntro
  字幕        字幕文件 / 编辑字幕 单选 + div[class*='upload_lrc_btn']「上传字幕文件」
              + 隐藏 input[type=file][accept='.lrc']
  权限设置    **在「更多设置 ∨」折叠面板里**，展开后是 input.ant-radio-input
              1=私密仅自己可见  2=公开所有人可见  3=仅好友可见
  发布按钮    button.confirm-publish（文字「确认发布」）

三条踩过的坑（都曾导致静默失败）：
1. 权限设置默认折叠，radio 的尺寸是 0×0。折叠时去点它等于点了个看不见的模板，
   平台真实的可见性仍然是「公开所有人可见」。必须先展开「更多设置」。
2. 专辑选择器是精确文本匹配，账号里的专辑名和套包 title 不同就选不中，
   选不中还会把整个选择器弹层留在页面上，挡住后面的表单操作。
3. 平台一次只收**一个**字幕文件，双语要用 ` | ` 隔开写进同一个 LRC。

设计要点：
- 关键步骤均做"确认"而非"假设"：选中专辑后回读标题、点完权限后回读
  ant-radio-checked、发布后回读 URL/文案，避免静默失败。
- 平台有风控（验证码/操作频繁），命中即中止当前批次并抛错，交给人工处理。
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout

from .package import Chapter, Package, iter_subtitle_tracks, merge_bilingual_lrc

UPLOAD_URL = "https://www.ximalaya.com/reform-upload/page/webCenter/upload"

# 平台 radio 的真实取值（实测）：
#   1 = 私密仅自己可见   2 = 公开所有人可见   3 = 仅好友可见只有相互关注可见
VISIBILITY_MAP = {"private": "1", "public": "2", "fans": "3"}
VISIBILITY_LABEL = {
    "private": "私密仅自己可见",
    "public": "公开所有人可见",
    "fans": "仅好友可见",
}

# 命中即代表平台风控介入，应停止自动化交由人工处理
_BLOCK_TEXTS = [
    "验证码",
    "安全验证",
    "请完成安全验证",
    "操作过于频繁",
    "访问受限",
    "人机验证",
    "请验证你是人类",
    "网络异常请稍后重试",
    "您的操作过于频繁",
]


class UploadError(Exception):
    """上传流程失败。"""


class RiskControl(UploadError):
    """平台风控介入，需要人工介入。"""


# --------------------------------------------------------------------------
# 专辑名匹配
# --------------------------------------------------------------------------

_PUNCT = re.compile(r"[\s《》「」『』（）()·:：,，。.!！?？\"'“”‘’\-—_]+")


def _normalize(s: str) -> str:
    """去掉标点与空白，用于宽松比较专辑名。"""
    return _PUNCT.sub("", s or "").lower()


def _longest_common(a: str, b: str) -> tuple[int, bool, bool]:
    """最长公共子串长度，以及它是否分别是 a / b 的前缀。"""
    if not a or not b:
        return 0, False, False
    prev = [0] * (len(b) + 1)
    best = 0
    best_i = best_j = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
                    best_i = i - cur[j]
                    best_j = j - cur[j]
        prev = cur
    return best, best_i == 0, best_j == 0


def _score_album(wanted: str, candidate: str) -> tuple[int, str]:
    """给「套包名 ↔ 专辑名」这一对打一个可比的分。返回 (分数, 理由)。"""
    w = _normalize(wanted)
    n = _normalize(candidate)
    if not w or not n:
        return 0, ""
    if w == n:
        return 1000, "完全匹配"

    score = 0
    reason = ""

    # 套包名按分隔符切段，整段出现在候选里才算强信号
    segments = [
        _normalize(seg) for seg in re.split(r"[·|｜]", wanted)
    ]
    segments = [s for s in segments if len(s) >= 2]
    segments.sort(key=len, reverse=True)
    for seg in segments:
        if seg in n:
            score += 100 + len(seg) * 10
            reason = f"书名片段命中（{seg}）"
            break

    # 最长公共子串
    length, prefix_w, prefix_c = _longest_common(w, n)
    if length >= 4:
        score += length + (10 if (prefix_w or prefix_c) else 0)
        if not reason:
            reason = f"公共片段 {length} 字" + ("（书名开头）" if prefix_w or prefix_c else "")

    return score, reason


def _match_album(
    wanted: str, candidates: list[str]
) -> tuple[str | None, str, list[str]]:
    """在候选专辑名里找最接近 `wanted` 的一个。

    返回 (匹配到的专辑名 or None, 判定理由, 同样匹配的候选列表)。

    **并列时不猜**：账号里可能有两个专辑都含「绿野仙踪」这类共同片段
    （例如「睡前有声读物：《绿野仙踪》」和「Tobeart有声书 | 绿野仙踪｜剧场版｜全集」），
    这时任选其一会把内容静默传到错误的专辑 —— 必须让用户明确指定。
    """
    if not candidates:
        return None, "无可用专辑", []

    scored = [(_score_album(wanted, c)[0], c) for c in candidates]
    best = max(s for s, _ in scored)
    if best <= 0:
        return None, "没有可匹配的专辑", []

    winners = [c for s, c in scored if s == best]
    reason = _score_album(wanted, winners[0])[1]
    if len(winners) == 1:
        return winners[0], reason, winners
    return (
        None,
        f"有 {len(winners)} 个专辑同样匹配（{reason}），需要手动指定",
        winners,
    )


# --------------------------------------------------------------------------
# 结果与状态
# --------------------------------------------------------------------------


@dataclass
class ChapterResult:
    """单章上传结果。"""

    chapter_id: str
    title: str
    ok: bool = False
    message: str = ""
    track_id: str | None = None
    url: str | None = None
    elapsed: float = 0.0
    album_missing: bool = False  # 专辑未找到（降级填单）
    published: bool = False  # 是否真的发布成功（草稿模式为 False）
    fingerprint: str = ""  # 章节内容指纹（判断套包是否已更新）


class BrowserGone(UploadError):
    """浏览器连接断了 —— 再传下去只会一章章地失败，必须停下来。"""


# Playwright 报"连接/目标已关闭"的几种说法
_DEAD_BROWSER_HINTS = (
    "connection closed",
    "browser has been closed",
    "target closed",
    "browser closed",
    "websocket",
    "connection reset",
)


def _is_browser_dead(exc: Exception) -> bool:
    """判断异常是不是"浏览器/连接没了"。"""
    text = str(exc).lower()
    return any(h in text for h in _DEAD_BROWSER_HINTS)


# 全角空格（U+3000）与各种空白都要算作同一个分隔符
_WS_RE = re.compile(r"[\s\u3000\u00a0]+")


def norm_title(title: str) -> str:
    """标题归一化，用来和平台上的章节标题比对。

    平台上的标题是用户自己填过的，空格可能是全角（"第1章　龙卷风"）
    也可能是半角（"第1章 龙卷风"），不归一化就会把同一章当成两章，
    于是重复上传，或者该跳过的没跳过。
    """
    return _WS_RE.sub(" ", (title or "").strip()).lower()


@dataclass
class UploadPlan:
    """一次批量上传的配置。"""

    album_name: str
    visibility: str = "private"  # private / public / fans
    description_template: str = ""
    subtitle_mode: str = "none"  # none / lrc / srt
    publish: bool = True  # True=自动点「确认发布」；False=人工确认模式
    manual_timeout: int = 600  # 人工确认模式下等待「确认发布」的秒数
    interval_range: tuple[float, float] = (8.0, 18.0)
    upload_timeout: int = 600
    create_album: bool = False
    cover_image: Path | None = None
    ai_synthetic: str = "yes"  # yes / no（平台「是否AI合成」）


@dataclass
class UploadState:
    """断点续传状态。

    除了"哪一章传过"，还记着**这份状态属于哪个套包**（package_key）
    以及每章的**内容指纹**（fingerprint）。

    没有这两样的话，套包一更新（重新配音、重新出图，章节 id 和标题却不变）
    旧状态就会把新内容整批跳过 —— 表现是"跑完了，却一章都没传上去"。
    """

    album_name: str = ""
    package_key: str = ""
    done: dict[str, ChapterResult] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "album_name": self.album_name,
                "package_key": self.package_key,
                "done": {
                    k: {
                        "ok": v.ok,
                        "published": v.published,
                        "message": v.message,
                        "track_id": v.track_id,
                        "url": v.url,
                        "fingerprint": v.fingerprint,
                        "title": v.title,
                    }
                    for k, v in self.done.items()
                },
            },
            ensure_ascii=False,
            indent=2,
        )

    @classmethod
    def from_json(cls, raw: str) -> "UploadState":
        data = json.loads(raw)
        st = cls(
            album_name=data.get("album_name", ""),
            package_key=data.get("package_key", ""),
        )
        for cid, v in (data.get("done") or {}).items():
            st.done[cid] = ChapterResult(
                chapter_id=cid,
                title=v.get("title", ""),
                ok=bool(v.get("ok")),
                published=bool(v.get("published")),
                message=v.get("message", ""),
                track_id=v.get("track_id"),
                url=v.get("url"),
                fingerprint=v.get("fingerprint", ""),
            )
        return st

    def matches_package(self, package_key: str) -> bool:
        """这份状态是不是当前套包留下的。

        旧版本的状态文件没有 package_key —— 那就退回逐章指纹比对，
        没有指纹的旧记录一律当作"对不上"（宁可多传一次，也不要静默跳过）。
        """
        if not self.package_key:
            return False
        return self.package_key == package_key


# --------------------------------------------------------------------------
# 页面内脚本
# --------------------------------------------------------------------------

# 读取「权限设置」里的 radio（只认真实可见的那些）
_JS_VIS_READ = """() => {
    const items = Array.from(document.querySelectorAll('.ant-form-item')).filter(it => {
        const l = it.querySelector('.ant-form-item-label');
        return l && /权限设置/.test(l.innerText);
    });
    if (!items.length) return {err: '权限设置 form item not found', list: []};
    const radios = Array.from(items[0].querySelectorAll('input.ant-radio-input'));
    const list = radios.map(r => {
        const lab = r.closest('label.ant-radio-wrapper');
        const rect = lab ? lab.getBoundingClientRect() : null;
        const wrap = r.closest('.ant-radio-wrapper');
        const dot = wrap ? wrap.querySelector('.ant-radio') : null;
        return {
            value: r.value,
            label: lab ? lab.innerText.trim() : '',
            visible: !!(rect && rect.width > 0 && rect.height > 0),
            checked: dot ? dot.classList.contains('ant-radio-checked') : r.checked,
        };
    });
    const cur = list.find(x => x.checked);
    return {list: list, value: cur ? cur.value : null, label: cur ? cur.label : null};
}"""

# 点击指定 value 的 radio
_JS_VIS_CLICK = """(value) => {
    const items = Array.from(document.querySelectorAll('.ant-form-item')).filter(it => {
        const l = it.querySelector('.ant-form-item-label');
        return l && /权限设置/.test(l.innerText);
    });
    if (!items.length) return {ok: false, reason: '权限设置 form item not found'};
    const radios = Array.from(items[0].querySelectorAll('input.ant-radio-input'));
    const target = radios.find(r => r.value === value);
    if (!target) return {ok: false, reason: 'radio value=' + value + ' not found'};
    const lab = target.closest('label.ant-radio-wrapper');
    if (lab) lab.click(); else target.click();
    return {ok: true, label: lab ? lab.innerText.trim() : ''};
}"""

# 展开「更多设置 ∨」折叠面板
_JS_EXPAND_MORE = """() => {
    const cands = Array.from(document.querySelectorAll('div,span,button,a')).filter(e => {
        const t = (e.innerText || '').trim();
        if (!t || t.length > 14) return false;
        if (!/更多设置/.test(t)) return false;
        const r = e.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    });
    if (!cands.length) return {ok: false, reason: '未找到「更多设置」'};
    // 取最内层的那个（文本最纯），点击它的可点击祖先
    const el = cands[cands.length - 1];
    const clickable = el.closest('button,a,[class*=more],[class*=fold],[class*=collapse]') || el;
    clickable.click();
    return {ok: true, text: (el.innerText || '').trim()};
}"""

# 读取专辑弹层里的专辑名
_JS_ALBUM_TITLES = """() => {
    const out = [];
    for (const el of document.querySelectorAll("span[class*='album-title-text']")) {
        const t = (el.innerText || '').trim();
        if (t && !out.includes(t)) out.push(t);
    }
    return out;
}"""

# 读取「选择专辑」下拉当前显示的名字
_JS_ALBUM_CURRENT = """() => {
    const btn = document.querySelector("button[class*='search-select-album-btn']");
    if (!btn) return null;
    const t = (btn.innerText || '').trim();
    return t || null;
}"""

# 切到「编辑字幕」模式（此时才出现 div.gen-lrc-box textarea）
_JS_SELECT_EDIT_SUBTITLE = """() => {
    const labs = Array.from(document.querySelectorAll('label.ant-radio-wrapper'));
    const lab = labs.find(l => (l.innerText || '').trim() === '编辑字幕');
    if (!lab) return {ok: false, reason: '未找到「编辑字幕」选项'};
    lab.click();
    return {ok: true};
}"""

# 按选项文字点 radio（用于「是否AI合成」这种取值不确定的组）
_JS_RADIO_BY_TEXT = """(labelText) => {
    const items = Array.from(document.querySelectorAll('.ant-form-item')).filter(it => {
        const l = it.querySelector('.ant-form-item-label');
        return l && l.innerText.includes('是否AI合成');
    });
    if (!items.length) return {ok: false, reason: '未找到「是否AI合成」选项组'};
    const radios = Array.from(items[0].querySelectorAll('input.ant-radio-input'));
    for (const r of radios) {
        const lab = r.closest('label.ant-radio-wrapper');
        if (!lab) continue;
        if (lab.innerText.trim() === labelText) {
            lab.click();
            return {ok: true, label: lab.innerText.trim(), value: r.value};
        }
    }
    return {ok: false, reason: '没有文字为「' + labelText + '」的选项'};
}"""


class XimalayaUploader:
    """在 Playwright 页面对象上执行上传。"""

    def __init__(
        self,
        page: Any,
        *,
        log: Callable[[str], None] | None = None,
        on_progress: Callable[[str, int, int], None] | None = None,
        session: Any = None,
    ) -> None:
        self.page = page
        self._log = log or (lambda m: None)
        self._progress = on_progress or (lambda cid, i, n: None)
        # 可选：拿到 BrowserSession 后，浏览器连接断了能自己重连
        self._session = session
        self._last_create_payload: dict[str, Any] | None = None
        self._expect_subtitle = False
        self._on_request = self._capture_create

    def _recover_browser(self) -> bool:
        """浏览器连接断了以后尝试恢复（重连 / 重新接管）。"""
        if self._session is None:
            return False
        try:
            ok = bool(self._session.reconnect())
        except Exception as exc:  # noqa: BLE001
            self._log(f"    ⚠ 重新连接浏览器失败：{exc}")
            return False
        if ok:
            self.page = self._session.page
            self._log("    ✅ 浏览器已重新连接，继续上传")
        return ok

    def _capture_create(self, req: Any) -> None:
        """记录发布请求 `upload/create` 的 payload，用于事后核对字段。"""
        if req.method != "POST" or "/upload/create" not in req.url:
            return
        try:
            body = req.post_data or ""
            self._last_create_payload = json.loads(body)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 工具

    def _body_text(self) -> str:
        try:
            return self.page.evaluate("() => document.body.innerText") or ""
        except Exception:  # noqa: BLE001
            return ""

    def _blocked(self) -> bool:
        text = self._body_text()
        return any(k in text for k in _BLOCK_TEXTS)

    def _check_block(self) -> None:
        if self._blocked():
            raise RiskControl(
                "平台触发风控（验证码 / 操作过于频繁），已中止上传。请稍后在浏览器中"
                "手动完成验证或降低上传频率后重试。"
            )

    def _wait_upload_done(self, timeout_sec: int = 300) -> bool:
        """轮询直到「上传中」消失。"""
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if "上传中" not in self._body_text():
                return True
            self.page.wait_for_timeout(1500)
        return False

    # ------------------------------------------------------------ 专辑

    def _album_picker_button(self):
        """定位「选择专辑」下拉按钮。"""
        candidates = [
            "button[class*='search-select-album-btn']",
            "button[class*='select-album-btn']",
            "button:has-text('选择专辑')",
            "button:has-text('添加到专辑')",
            "button:has-text('选择作品目录')",
        ]
        for sel in candidates:
            try:
                loc = self.page.locator(sel).first
                if loc.count() > 0:
                    return loc
            except (PWError, PWTimeout):
                continue
        return None

    def _open_album_picker(self) -> bool:
        btn = self._album_picker_button()
        if btn is None:
            return False
        try:
            btn.click(timeout=15_000)
            self.page.wait_for_timeout(1200)
            return True
        except (PWError, PWTimeout):
            try:
                btn.click(timeout=8000, force=True)
                self.page.wait_for_timeout(1200)
                return True
            except (PWError, PWTimeout):
                return False

    def _close_album_picker(self) -> None:
        """关闭专辑选择弹层。

        不关掉的话，弹层会一直盖在表单上，后面的标题/权限/字幕操作都点不动。
        """
        try:
            self.page.keyboard.press("Escape")
        except (PWError, PWTimeout):
            pass
        self.page.wait_for_timeout(600)

    def list_available_albums(self) -> list[str]:
        """列出账号里可选的专辑名（供界面选择）。

        优先走接口 —— 上传页上的专辑下拉要等文件上传成功才出现，
        没法在"还没开始传"的时候查看自己有哪些专辑。
        """
        from .album import list_albums

        albums = list_albums(self.page)
        if albums:
            return [a["title"] for a in albums]

        # 兜底：打开页面上那个专辑弹层来读
        if not self._open_album_picker():
            return []
        titles = self._album_titles()
        self._close_album_picker()
        return titles

    def _album_titles(self) -> list[str]:
        try:
            titles = self.page.evaluate(_JS_ALBUM_TITLES) or []
        except (PWError, PWTimeout):
            titles = []
        # 弹层里可能有"加载更多"，翻页把全部专辑拉出来
        for _ in range(12):
            more = self.page.locator(
                "button:has-text('加载更多'), div:has-text('加载更多')"
            ).first
            try:
                if more.count() > 0 and more.is_visible():
                    before = len(titles)
                    more.click(timeout=5000)
                    self.page.wait_for_timeout(1200)
                    titles = self.page.evaluate(_JS_ALBUM_TITLES) or []
                    if len(titles) == before:
                        break
                    continue
            except (PWError, PWTimeout):
                pass
            break
        return [t for t in titles if t and "不进专辑" not in t and "创建新专辑" not in t]

    def select_album(self, album_name: str, timeout_ms: int = 30_000) -> str:
        """在专辑选择器里选专辑。

        找不到精确同名时会做模糊匹配（见 _match_album），
        并在日志里说明命中依据——避免"看起来传了、其实没归属"。
        """
        if not self._open_album_picker():
            raise UploadError("未找到专辑选择按钮（页面结构可能已变化）")

        titles = self._album_titles()
        target, reason, candidates = _match_album(album_name, titles)

        if target is None and candidates:
            # 有并列候选 → 不能猜，否则会静默传到错误的专辑
            self._close_album_picker()
            raise UploadError(
                f"《{album_name}》同时匹配到多个专辑，请用界面上的「选择…」明确指定一个：\n"
                + "\n".join(f"    · {c}" for c in candidates)
            )

        if target is None:
            # 兜底：在弹层的搜索框里搜关键词
            target = self._search_album(album_name)
            reason = "搜索框模糊匹配" if target else "无匹配"

        if target is None:
            self._close_album_picker()
            raise UploadError(
                f"专辑选择器中找不到《{album_name}》。账号现有专辑：{titles[:20]}"
            )

        loc = self.page.locator(
            f"span[class*='album-title-text']:text-is('{target}')"
        ).first
        try:
            loc.click(timeout=10_000)
        except (PWError, PWTimeout):
            try:
                loc.click(timeout=8000, force=True)
            except (PWError, PWTimeout) as exc:
                self._close_album_picker()
                raise UploadError(f"点击专辑《{target}》失败：{exc}") from exc
        self.page.wait_for_timeout(800)
        self._close_album_picker()

        # 回读确认：下拉框里现在应该显示这个专辑
        current = None
        try:
            current = self.page.evaluate(_JS_ALBUM_CURRENT)
        except (PWError, PWTimeout):
            pass
        if current and _normalize(current) != _normalize(target):
            raise UploadError(
                f"专辑选择后未生效：下拉显示《{current}》，期望《{target}》"
            )

        if reason == "完全匹配":
            self._log(f"    已选择专辑：{target}")
        else:
            self._log(f"    已选择专辑：{target}（{reason}，套包名《{album_name}》）")
        return target

    def _search_album(self, keyword: str) -> str | None:
        """用弹层里的搜索框找专辑。"""
        try:
            box = self.page.locator(
                "input[placeholder='搜索你想要找的专辑']"
            ).first
            if box.count() == 0:
                return None
            self.page.evaluate(
                """(kw) => {
                    const i = document.querySelector(
                        "input[placeholder='搜索你想要找的专辑']");
                    if (!i) return;
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    setter.call(i, kw);
                    i.dispatchEvent(new Event('input', {bubbles: true}));
                    i.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                keyword,
            )
            self.page.wait_for_timeout(2500)
            titles = self.page.evaluate(_JS_ALBUM_TITLES) or []
            if not titles:
                return None
            target, _reason, _cands = _match_album(keyword, titles)
            return target
        except (PWError, PWTimeout):
            return None

    # ------------------------------------------------------------ 表单

    def set_title(self, title: str) -> None:
        inp = self.page.locator("input.ant-input[placeholder='请输入声音标题']")
        inp.click(timeout=15_000)
        inp.fill("")
        inp.fill(title)
        self.page.wait_for_timeout(300)

    def set_description(self, desc: str) -> dict[str, Any]:
        """通过 KindEditor 写入简介（富文本，换行需转 <br>）。"""
        return self.page.evaluate(
            """(desc) => {
                const KE = window.KindEditor;
                const editor = (KE && KE.instances) ? KE.instances['0'] : null;
                if (!editor) return {ok:false, reason:'KindEditor instance not found'};
                const esc = desc.replace(/&/g,'&amp;').replace(/</g,'&lt;')
                               .replace(/>/g,'&gt;');
                const html = esc.replace(/\\n/g, '<br>');
                editor.html(html);
                try { KE.sync('soundRichIntro'); } catch(e) {}
                try { editor.sync(); } catch(e) {}
                const t = document.querySelector('textarea#soundRichIntro');
                return {ok:true, len:desc.length, synced: t ? t.value.length : 0};
            }""",
            desc,
        )

    # ---------------------------------------------------------- 权限设置

    def _expand_more_settings(self) -> bool:
        """展开「更多设置 ∨」。

        权限设置藏在折叠面板里，折叠时那些 radio 的尺寸是 0×0，
        点它们等于点了个看不见的模板 —— 平台真实可见性纹丝不动。
        """
        state = self.page.evaluate(_JS_VIS_READ) or {}
        visible = [r for r in (state.get("list") or []) if r.get("visible")]
        if visible:
            return True
        try:
            r = self.page.evaluate(_JS_EXPAND_MORE) or {}
        except (PWError, PWTimeout):
            return False
        if not r.get("ok"):
            return False
        self.page.wait_for_timeout(1200)
        state = self.page.evaluate(_JS_VIS_READ) or {}
        visible = [r for r in (state.get("list") or []) if r.get("visible")]
        if visible:
            self._log("    已展开「更多设置」以设置权限")
            return True
        return False

    def set_visibility(self, visibility: str = "private") -> dict[str, Any]:
        """设置可见性并回读确认。

        平台默认是「公开所有人可见」，必须显式改，而且必须先展开「更多设置」，
        否则改的是一个隐藏的模板节点，用户看到的内容仍然是公开的。
        """
        value = VISIBILITY_MAP.get(visibility, "1")
        last_reason = "未知"

        for attempt in range(3):
            self._expand_more_settings()
            try:
                r = self.page.evaluate(_JS_VIS_CLICK, value) or {}
            except (PWError, PWTimeout) as exc:
                last_reason = f"点击失败：{exc}"
                continue
            if not r.get("ok"):
                last_reason = r.get("reason", "点击失败")
                continue

            # 等一拍再回读：平台在音频上传结束时会重置整个表单，
            # 太早回读会读到还没落定的状态。
            self.page.wait_for_timeout(800)
            state = self.page.evaluate(_JS_VIS_READ) or {}
            cur = state.get("value")
            if cur == value:
                return {
                    "ok": True,
                    "labelText": state.get("label") or VISIBILITY_LABEL.get(visibility),
                    "value": value,
                }
            last_reason = f"回读为 {state.get('label') or cur}（期望 {value}）"

        return {
            "ok": False,
            "reason": last_reason,
            "labelText": VISIBILITY_LABEL.get(visibility, ""),
        }

    # ---------------------------------------------------------- AI 合成标记

    def set_ai_synthetic(self, flag: str = "yes") -> dict[str, Any]:
        """设置「是否AI合成」。

        平台默认是「否」。套包 source 里记着合成模型时应该标「是」，
        否则等于把合成音频误报成真人录音。
        """
        label = "是" if flag in ("yes", "是", "true", True) else "否"
        for _ in range(3):
            try:
                r = self.page.evaluate(_JS_RADIO_BY_TEXT, label) or {}
            except (PWError, PWTimeout) as exc:
                return {"ok": False, "reason": f"点击失败：{exc}"}
            if not r.get("ok"):
                return {"ok": False, "reason": r.get("reason", "未知")}
            self.page.wait_for_timeout(500)
            return {"ok": True, "labelText": r.get("label"), "value": r.get("value")}
        return {"ok": False, "reason": "重试仍失败"}

    def _accept_agreement(self) -> None:
        """勾选《知识产权承诺》。不勾选时「确认发布」按钮保持禁用，会静默失败。"""
        self.page.evaluate(
            """() => {
                const labels = Array.from(document.querySelectorAll('label,span,div'));
                const hit = labels.find(el => el.innerText
                    && el.innerText.includes('知识产权承诺'));
                const cb = hit ? hit.querySelector('input[type=checkbox]') : null;
                if (cb && !cb.checked) {
                    cb.checked = true;
                    cb.dispatchEvent(new Event('change', {bubbles:true}));
                    cb.dispatchEvent(new Event('click', {bubbles:true}));
                }
            }"""
        )
        self.page.wait_for_timeout(600)

    # ------------------------------------------------------------ 字幕

    def _subtitle_attached(self, files: list[Path]) -> bool:
        """回读字幕是否真的挂上了。

        注意：平台把字幕解析进「编辑字幕」的 textarea，**不会**显示文件名，
        而且 textarea 的内容是表单值，不在 innerText 里 —— 所以只能读 value。
        """
        return bool(
            self.page.evaluate(
                """() => {
                    const items = Array.from(document.querySelectorAll('.ant-form-item'))
                        .filter(it => {
                            const l = it.querySelector('.ant-form-item-label');
                            return l && /字幕/.test(l.innerText);
                        });
                    for (const it of items) {
                        for (const ta of it.querySelectorAll('textarea')) {
                            if (ta.offsetParent !== null &&
                                (ta.value || '').trim().length > 10) return true;
                        }
                    }
                    for (const ta of document.querySelectorAll('textarea')) {
                        if (ta.offsetParent !== null &&
                            /\\[\\d+:\\d+/.test(ta.value || '')) return true;
                    }
                    return false;
                }"""
            )
        )

    def _attach_subtitle(self, chapter: Chapter, plan: UploadPlan) -> bool:
        """挂载字幕。

        平台的字幕不是"选个文件"那么简单，正确流程是：

            1. 切到「编辑字幕」模式（此时才出现 `div.gen-lrc-box textarea`）
            2. 把 LRC 文本**用真实输入事件**写进 textarea
               （JS 直接赋值不触发平台自己的 onChange，字幕框会认不到内容）
            3. 点「确认提交」—— 平台把文本转成 lrc.json 传到对象存储，
               拿到路径后存进表单的 `lrcPath`
            4. **等上传完成再发布** —— 上传还没回来时 `lrcPath` 还是空的，
               立刻发布就会丢字幕

        第 4 步是之前一直没找到字幕的根因：fill 完就发布，`lrcPath` 是空串。
        """
        tracks = list(iter_subtitle_tracks(chapter))
        if not tracks:
            self._log("    ⚠ 未找到可用字幕轨，跳过字幕上传")
            return False

        tmp_dir = Path("_work") / "subtitles" / chapter.id
        tmp_dir.mkdir(parents=True, exist_ok=True)

        if len(tracks) >= 2:
            # 平台一次只收一个文件，双语要合并成一份、用 ` | ` 分隔
            content = merge_bilingual_lrc(tracks)
            path = tmp_dir / f"{chapter.id}.lrc"
        else:
            track = tracks[0]
            content = (
                track.to_srt() if plan.subtitle_mode == "srt" else track.to_lrc()
            )
            path = tmp_dir / f"{chapter.id}.{track.lang}.{'srt' if plan.subtitle_mode == 'srt' else 'lrc'}"
        path.write_text(content, encoding="utf-8")

        # 1) 切到「编辑字幕」
        try:
            picked = self.page.evaluate(_JS_SELECT_EDIT_SUBTITLE) or {}
        except (PWError, PWTimeout) as exc:
            self._log(f"    ⚠ 切换字幕模式失败：{exc}")
            return False
        if not picked.get("ok"):
            self._log(f"    ⚠ {picked.get('reason', '未找到「编辑字幕」')}")
            return False
        self.page.wait_for_timeout(1200)

        # 2) 写文本（用 fill 走真实输入事件）
        try:
            ta = self.page.locator("div.gen-lrc-box textarea").first
            ta.wait_for(state="visible", timeout=15_000)
            ta.click(timeout=10_000)
            ta.fill(content)
        except (PWError, PWTimeout) as exc:
            self._log(f"    ⚠ 写字幕内容失败：{exc}")
            return False
        self.page.wait_for_timeout(1200)

        # 3) 点「确认提交」，并等平台把字幕传到对象存储
        #    （页面上没有任何"提交成功"的提示，只能靠请求判断）
        btn = self.page.locator("button[aria-label='确认提交字幕']").first
        seen: list[str] = []

        def _watch(req) -> None:
            u = req.url
            if "clamper-token" in u or "file/blk" in u or "mkfile" in u:
                seen.append(u)

        try:
            if btn.count() == 0:
                self._log("    ⚠ 未找到「确认提交」按钮")
                return False
        except (PWError, PWTimeout):
            self._log("    ⚠ 未找到「确认提交」按钮")
            return False

        self.page.on("request", _watch)
        try:
            btn.click(timeout=10_000)
            deadline = time.time() + 90
            while time.time() < deadline:
                if any("mkfile" in u for u in seen):
                    break
                if any("file/blk" in u for u in seen):
                    # 单 chunk 直传：没有 merge 步骤，等平台收尾即可
                    self.page.wait_for_timeout(4000)
                    if not any("mkfile" in u for u in seen):
                        break
                self.page.wait_for_timeout(500)
            # 让平台的 React state 拿到 lrcPath
            self.page.wait_for_timeout(3000)
        except (PWError, PWTimeout) as exc:
            self._log(f"    ⚠ 点击「确认提交」失败：{exc}")
            return False
        finally:
            try:
                self.page.remove_listener("request", _watch)
            except Exception:  # noqa: BLE001
                pass

        if not any("mkfile" in u or "file/blk" in u for u in seen):
            self._log(
                f"    ⚠ 字幕未提交成功（平台没有收到上传请求），"
                f"文件已生成在 {path}，发布后需手动补传"
            )
            return False

        self._log(f"    ✅ 已提交字幕（{path.name}，{len(content)} 字符）")
        return True

    # ------------------------------------------------------------ 单章

    def upload_chapter(
        self,
        chapter: Chapter,
        plan: UploadPlan,
        *,
        index: int = 1,
        total: int = 1,
    ) -> ChapterResult:
        """上传一章音频（含标题/简介/专辑/权限/字幕），返回结果。"""
        try:
            return self._upload_chapter_once(chapter, plan, index=index, total=total)
        except BrowserGone:
            raise
        except (PWError, PWTimeout) as exc:
            if not _is_browser_dead(exc):
                result = ChapterResult(
                    chapter_id=chapter.id,
                    title=chapter.title,
                    ok=False,
                    message=f"浏览器异常：{exc}",
                    fingerprint=chapter.fingerprint,
                )
                self._log(f"    ❌ 浏览器异常：{exc}")
                return result

            # 连接断了：先试着接回来，再重传这一章
            self._log(f"    ⚠ 浏览器连接断开（{exc}），正在尝试重连…")
            if self._recover_browser():
                try:
                    return self._upload_chapter_once(
                        chapter, plan, index=index, total=total
                    )
                except BrowserGone:
                    raise
                except (PWError, PWTimeout) as exc2:
                    if _is_browser_dead(exc2):
                        raise BrowserGone(f"浏览器连接断开且无法恢复：{exc2}") from exc2
                    result = ChapterResult(
                        chapter_id=chapter.id,
                        title=chapter.title,
                        ok=False,
                        message=f"浏览器异常：{exc2}",
                        fingerprint=chapter.fingerprint,
                    )
                    self._log(f"    ❌ 浏览器异常：{exc2}")
                    return result
            raise BrowserGone(f"浏览器连接断开且无法恢复：{exc}") from exc

    def _upload_chapter_once(
        self,
        chapter: Chapter,
        plan: UploadPlan,
        *,
        index: int = 1,
        total: int = 1,
    ) -> ChapterResult:
        started = time.time()
        result = ChapterResult(
            chapter_id=chapter.id,
            title=chapter.title,
            fingerprint=chapter.fingerprint,
        )

        self._progress(chapter.id, index, total)
        self._log(f"[{index}/{total}] 上传 {chapter.id} · {chapter.title}")

        try:
            self.page.goto(UPLOAD_URL, timeout=60_000)
            self.page.wait_for_timeout(2500 + random.randint(0, 1200))
            self._check_block()

            # 1) 选文件 —— 点击后弹原生文件选择器，捕获并喂入文件
            pick = self.page.locator("div.webuploader-pick", has_text="上传音频").first
            pick.wait_for(timeout=20_000)
            with self.page.expect_file_chooser(timeout=15_000) as fc:
                pick.click()
            fc.value.set_files(str(chapter.audio))
            self.page.wait_for_timeout(1500)

            # 确认文件真的被接受
            name = chapter.audio.name
            try:
                self.page.wait_for_function(
                    "() => { const t = document.body.innerText; "
                    f"return t.includes({json.dumps(name)}) || t.includes('上传成功'); }}",
                    timeout=60_000,
                )
            except (PWTimeout, PWError):
                raise UploadError("文件选择器未接受该文件（可能格式或大小不被接受）")

            if not self._wait_upload_done(plan.upload_timeout):
                raise UploadError("等待音频上传完成超时")
            self._check_block()
            # 表单在上传完成那刻才真正可交互，再给它一点时间渲染
            self.page.wait_for_timeout(1500)

            # 2) 专辑
            if plan.album_name:
                try:
                    self.select_album(plan.album_name)
                except UploadError as exc:
                    # 专辑尚未创建时不中断整批：提示并继续填单，
                    # 由用户在创作中心补建专辑，或用「新建」按钮先建。
                    self._log(f"    ⚠ {exc}")
                    self._log("    ⚠ 将继续填单（未归属专辑），请稍后在创作中心补建/指定专辑")
                    result.album_missing = True

            # 3) 标题
            self.set_title(chapter.title)

            # 3.5) 是否AI合成（平台默认「否」，合成音频应标「是」）
            if plan.ai_synthetic:
                r = self.set_ai_synthetic(plan.ai_synthetic)
                if r.get("ok"):
                    self._log(f"    AI合成标记：{r.get('labelText')}")
                else:
                    self._log(f"    ⚠ AI合成标记未设置：{r.get('reason')}")

            # 4) 简介
            desc = self._build_description(chapter, plan)
            if desc:
                r = self.set_description(desc)
                if not r.get("ok"):
                    raise UploadError(f"简介写入失败：{r.get('reason')}")

            # 5) 权限（必须在「更多设置」展开后再改，否则改的是隐藏模板）
            r = self.set_visibility(plan.visibility)
            if not r.get("ok"):
                raise UploadError(f"可见性设置失败：{r.get('reason')}")
            self._log(f"    可见性：{r.get('labelText') or VISIBILITY_LABEL.get(plan.visibility)}")

            # 6) 字幕（若启用）
            self._expect_subtitle = plan.subtitle_mode != "none"
            if plan.subtitle_mode != "none":
                self._attach_subtitle(chapter, plan)

            self.page.wait_for_timeout(random.randint(600, 1600))
            self._check_block()

            # 7) 发布
            self._accept_agreement()
            self.page.wait_for_timeout(random.randint(500, 1400))

            if not plan.publish:
                # 「人工确认」模式：**喜马拉雅没有草稿箱**。
                # 点「继续添加」只是把这一条留在当前页面的待发布列表里，
                # 服务端不存任何东西 —— 下一章重新加载页面就全没了。
                # 所以这里不点「继续添加」，而是把表单填好停住，
                # 等人工在浏览器里点「确认发布」，工具检测到成功再继续。
                self._log(
                    "    ⏸ 已填单，请在浏览器窗口点「确认发布」完成这一章"
                    f"（最多等 {plan.manual_timeout // 60} 分钟）"
                )
                ok, url = self._wait_manual_publish(plan.manual_timeout)
                result.elapsed = time.time() - started
                if ok:
                    result.ok = True
                    result.published = True
                    result.url = url
                    result.message = "发布成功（人工确认）"
                    self._log(f"    ✅ 发布成功 {url or ''}")
                else:
                    result.ok = False
                    result.published = False
                    result.message = (
                        "未发布：等待人工确认超时。本章**没有保存到喜马拉雅**，"
                        "重新运行会再传一次"
                    )
                    self._log(f"    ❌ {result.message}")
                return result

            ok, url = self._click_publish()
            if not ok:
                raise UploadError("发布未成功（按钮点击后未见成功跳转）")
            result.ok = True
            result.published = True
            result.url = url
            result.message = "发布成功"
            result.elapsed = time.time() - started
            self._log(f"    ✅ 发布成功 {url or ''}")
            return result

        except RiskControl:
            raise
        except BrowserGone:
            raise
        except (UploadError,) as exc:
            result.ok = False
            result.message = str(exc)
            result.elapsed = time.time() - started
            self._log(f"    ❌ {exc}")
            return result
        except (PWError, PWTimeout) as exc:
            if _is_browser_dead(exc):
                raise
            result.ok = False
            result.message = f"浏览器异常：{exc}"
            result.elapsed = time.time() - started
            self._log(f"    ❌ 浏览器异常：{exc}")
            return result

    def _build_description(self, chapter: Chapter, plan: UploadPlan) -> str:
        if plan.description_template:
            return (
                plan.description_template.replace("{title}", chapter.title)
                .replace("{id}", chapter.id)
            )
        return ""

    def _publish_button(self):
        for sel in (
            "button.confirm-publish",
            "button[class*='confirm-publish-btn']",
            "button:has-text('确认发布')",
        ):
            try:
                loc = self.page.locator(sel).first
                if loc.count() > 0:
                    return loc
            except (PWError, PWTimeout):
                continue
        return None

    def _wait_manual_publish(self, timeout_sec: int = 600) -> tuple[bool, str | None]:
        """等人工在浏览器里点「确认发布」。

        平台没有草稿箱，只有真正点下发布才会落到账号里。所以人工确认模式
        必须等到「确认发布」真的生效，否则这一章就等于没传。
        """
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                if "uploadSuccess" in self.page.url:
                    return True, self.page.url
            except (PWError, PWTimeout):
                pass
            if "发布成功" in self._body_text():
                return True, self.page.url
            self.page.wait_for_timeout(2000)
        return False, None

    def _click_publish(self, attempts: int = 4) -> tuple[bool, str | None]:
        """点击「确认发布」并确认成功。

        同时抓下 `POST /reform-upload/upload/create` 的 payload，
        用来核对可见性、专辑、字幕（lrcPath）这些关键字段有没有真的带上 ——
        发布接口不报错不代表字段对。
        """
        btn = self._publish_button()
        if btn is None:
            return False, None
        for _ in range(attempts):
            try:
                btn.wait_for(state="visible", timeout=15_000)
            except (PWTimeout, PWError):
                pass
            try:
                btn.click(timeout=10_000)
            except (PWError, PWTimeout):
                try:
                    btn.click(timeout=8000, force=True)
                except (PWError, PWTimeout):
                    pass

            # 平台可能再弹一层确认框
            self._dismiss_publish_confirm()

            try:
                self.page.wait_for_url(lambda u: "uploadSuccess" in u, timeout=20_000)
                self._check_publish_payload()
                return True, self.page.url
            except (PWTimeout, PWError):
                pass
            if "发布成功" in self._body_text():
                self._check_publish_payload()
                return True, self.page.url
        return False, None

    def _check_publish_payload(self) -> None:
        """核对最近一次发布请求里带的关键字段（从页面抓不到就跳过）。"""
        # 请求事件和「发布成功」的判定几乎同时发生，稍等一下再读
        deadline = time.time() + 5
        while self._last_create_payload is None and time.time() < deadline:
            self.page.wait_for_timeout(300)
        payload = self._last_create_payload
        if not payload:
            return
        self._last_create_payload = None
        vis = payload.get("visibleCrowdType")
        album = payload.get("albumId")
        lrc = payload.get("lrcPath") or ""
        self._log(
            f"    发布参数：专辑={album}　可见性={vis}"
            + (f"　字幕={'已带上' if lrc else '**缺失**'}" if self._expect_subtitle else "")
        )
        if self._expect_subtitle and not lrc:
            self._log(
                "    ⚠ 字幕没有随发布提交（lrcPath 为空），请到创作中心手动补传",
                "warn",
            )

    def _dismiss_publish_confirm(self) -> None:
        """处理发布后的二次确认弹窗。"""
        for text in ("确定", "确认发布", "确认"):
            try:
                dlg = self.page.locator(
                    f".ant-modal-wrap:visible button:has-text('{text}'), "
                    f".ant-popover:visible button:has-text('{text}')"
                ).first
                if dlg.count() > 0 and dlg.is_visible():
                    dlg.click(timeout=5000)
                    self.page.wait_for_timeout(1500)
                    return
            except (PWError, PWTimeout):
                continue

    def _click_continue_add(self) -> bool:
        """点「继续添加」。

        **不要用这个当"保存草稿"** —— 它只是把条目留在当前页面的待发布列表，
        服务端不存任何东西，刷新即丢。平台没有草稿箱。
        """
        for sel in ("button:has-text('继续添加')", "button[class*='continue-add-file']"):
            try:
                btn = self.page.locator(sel).first
                if btn.count() > 0:
                    btn.click(timeout=10_000)
                    self.page.wait_for_timeout(1500)
                    return True
            except (PWError, PWTimeout):
                continue
        return False

    # ------------------------------------------------------------ 批次

    def run_batch(
        self,
        pkg: Package,
        plan: UploadPlan,
        *,
        state: UploadState | None = None,
        resume: bool = True,
        on_result: Callable[[ChapterResult], None] | None = None,
        existing_titles: set[str] | None = None,
    ) -> list[ChapterResult]:
        """批量上传所有章节。

        断点续传只跳过**真正发布成功**且**内容没变**的章节：

        - 本地状态：记录了 ok + published，且章节指纹与当前套包一致；
          套包换了版本（指纹变了）就不算传过 —— 否则新内容会被整批跳过。
        - 平台实况：目标专辑里已经有同名章节，也跳过（平台才是权威）。

        草稿模式的条目不算完成，否则切到发布模式后会被全部跳过，
        等于什么都没传。
        """
        state = state or UploadState(album_name=plan.album_name)
        results: list[ChapterResult] = []
        # 发布请求的 payload 监听（只注册一次，否则每章都会叠加）
        try:
            self.page.on("request", self._on_request)
        except Exception:  # noqa: BLE001
            pass

        # ---- 断点状态与套包是否对得上 ----
        if state.package_key and state.package_key != pkg.package_key:
            self._log(
                f"⚠ 断点状态属于另一个套包版本（{state.package_key} → {pkg.package_key}），"
                "旧记录不再作为跳过依据"
            )
            state.done = {}
        elif not state.package_key and state.done:
            self._log(
                "⚠ 断点状态是旧格式（未记录套包版本），无法确认内容是否已传，"
                "改以「平台专辑里现有的章节」为准"
            )
        state.package_key = pkg.package_key

        existing = {
            norm_title(t) for t in (existing_titles or set()) if t and t.strip()
        }

        def _already_uploaded(ch: Chapter) -> tuple[bool, str]:
            """这一章是不是已经传过了？返回 (是, 依据)。"""
            if existing and norm_title(ch.title) in existing:
                return True, "专辑里已有同名章节"
            rec = state.done.get(ch.id)
            if not (rec and rec.ok and rec.published):
                return False, ""
            if rec.fingerprint and rec.fingerprint != ch.fingerprint:
                # 内容变了（重新配音/重新出图）→ 视为没传过
                return False, ""
            if not rec.fingerprint:
                # 旧状态没有指纹，没法证明内容一致 → 不跳过
                return False, ""
            return True, "断点状态记录已发布成功"

        todo: list[Chapter] = []
        skipped_reasons: dict[str, str] = {}
        for ch in pkg.chapters:
            done, why = _already_uploaded(ch)
            if resume and done:
                skipped_reasons[ch.id] = why
            else:
                todo.append(ch)

        skipped = len(skipped_reasons)
        if skipped:
            self._log(f"跳过 {skipped} 章（{_summarize_reasons(skipped_reasons)}）")
            self._log("    如需强制重传，可取消勾选「断点续传」")
            if any("同名章节" in w for w in skipped_reasons.values()):
                self._log(
                    "    ⚠ 目标专辑里已经有这批章节了；要换成新版本的内容，"
                    "请先到创作中心删除旧章节再传"
                )

        total = len(todo)
        for i, ch in enumerate(todo, 1):
            try:
                res = self.upload_chapter(ch, plan, index=i, total=total)
            except RiskControl:
                state.done[ch.id] = ChapterResult(
                    chapter_id=ch.id,
                    title=ch.title,
                    ok=False,
                    message="风控中止",
                    fingerprint=ch.fingerprint,
                )
                raise
            except BrowserGone as exc:
                state.done[ch.id] = ChapterResult(
                    chapter_id=ch.id,
                    title=ch.title,
                    ok=False,
                    message=str(exc),
                    fingerprint=ch.fingerprint,
                )
                if on_result:
                    on_result(state.done[ch.id])
                # 浏览器没了，继续传只会一章章地报同样的错 —— 立刻停下来
                raise
            results.append(res)
            state.done[ch.id] = res
            if on_result:
                on_result(res)
            if res.ok and i < total:
                lo, hi = plan.interval_range
                time.sleep(random.uniform(lo, hi))
        return results


def _summarize_reasons(reasons: dict[str, str]) -> str:
    """把跳过原因汇总成一句话。"""
    counts: dict[str, int] = {}
    for why in reasons.values():
        counts[why] = counts.get(why, 0) + 1
    return "；".join(f"{why} × {n}" for why, n in counts.items())

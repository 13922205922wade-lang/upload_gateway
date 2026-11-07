"""专辑创建。

创作中心的专辑创建页（/reform-upload/page/album/create）在登录后动态渲染，
class 名会随版本变化（如 ant-* 组件的 hash 后缀）。因此这里不写死选择器，
而是用**语义化发现**：按 placeholder / label 文本 / 元素类型定位表单字段，
再按按钮文字提交。

这样平台改版时多数情况无需改代码。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout

ALBUM_CREATE_URL = "https://www.ximalaya.com/reform-upload/page/album/create"
ALBUM_LIST_URL = "https://www.ximalaya.com/reform-upload/page/album/list"


class AlbumCreateError(Exception):
    """专辑创建失败。"""


@dataclass
class AlbumDraft:
    """专辑信息。"""

    name: str
    intro: str = ""
    cover: Path | None = None
    category: str = ""  # 运营分类，如「儿童」


def _fill_by_label(
    page: Any,
    labels: list[str],
    value: str,
    *,
    timeout: int = 12_000,
) -> bool:
    """按 placeholder / 相邻 label / aria-label 语义定位输入框并填值。

    返回是否成功。三个匹配通道任一命中即可。
    """
    if not value:
        return True

    # 通道 1：placeholder 包含关键词
    for kw in labels:
        try:
            loc = page.locator(f"input[placeholder*='{kw}'], textarea[placeholder*='{kw}']")
            if loc.count() > 0:
                loc.first.click(timeout=timeout)
                loc.first.fill("")
                loc.first.fill(value)
                page.wait_for_timeout(250)
                return True
        except (PWError, PWTimeout):
            continue

    # 通道 2：aria-label
    for kw in labels:
        try:
            loc = page.locator(f"input[aria-label*='{kw}'], textarea[aria-label*='{kw}']")
            if loc.count() > 0:
                loc.first.click(timeout=timeout)
                loc.first.fill(value)
                page.wait_for_timeout(250)
                return True
        except (PWError, PWTimeout):
            continue

    # 通道 3：form-item label 文本相邻的 input
    for kw in labels:
        try:
            ok = page.evaluate(
                """(kw) => {
                    const items = Array.from(document.querySelectorAll(
                        '.ant-form-item, .form-item, [class*=form-item]'));
                    for (const it of items) {
                        const lb = it.querySelector('label, .ant-form-item-label');
                        if (!lb || !lb.innerText.includes(kw)) continue;
                        const inp = it.querySelector(
                            'input:not([type=file]), textarea');
                        if (!inp) continue;
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value')
                            || Object.getOwnPropertyDescriptor(
                            window.HTMLTextAreaElement.prototype, 'value');
                        if (setter && inp instanceof HTMLInputElement) {
                            setter.set.call(inp, kw);
                        } else if (inp instanceof HTMLTextAreaElement) {
                            Object.getOwnPropertyDescriptor(
                                window.HTMLTextAreaElement.prototype, 'value')
                                .set.call(inp, kw);
                        } else {
                            inp.value = kw;
                        }
                        inp.dispatchEvent(new Event('input', {bubbles:true}));
                        inp.dispatchEvent(new Event('change', {bubbles:true}));
                        inp.focus();
                        return true;
                    }
                    return false;
                }""",
                kw,
            )
            if ok:
                page.wait_for_timeout(250)
                return True
        except Exception:  # noqa: BLE001
            continue

    return False


def _set_cover(page: Any, cover: Path, *, timeout: int = 15_000) -> bool:
    """上传专辑封面（找到第一个接受图片的 file input）。"""
    if not cover or not cover.is_file():
        return False
    inputs = page.locator("input[type=file]")
    try:
        n = inputs.count()
    except Exception:  # noqa: BLE001
        return False
    for i in range(min(n, 5)):
        try:
            inp = inputs.nth(i)
            accept = (inp.get_attribute("accept") or "").lower()
            if accept and not ("image" in accept or accept.startswith(".")):
                continue
            inp.set_input_files(str(cover))
            page.wait_for_timeout(1500)
            return True
        except (PWError, PWTimeout):
            continue
    return False


def create_album(
    page: Any,
    draft: AlbumDraft,
    *,
    log: Callable[[str], None] | None = None,
    timeout: int = 90_000,
) -> str:
    """在创作中心创建一个专辑，返回成功提示文本。

    流程：打开创建页 → 填名称/简介 → 上传封面 → 选分类 → 提交。
    """
    _log = log or (lambda m: None)
    _log(f"打开专辑创建页：{ALBUM_CREATE_URL}")
    page.goto(ALBUM_CREATE_URL, timeout=60_000)
    page.wait_for_timeout(2500)

    body = page.inner_text("body")
    if "请使用喜马拉雅APP扫码登录" in body:
        raise AlbumCreateError("创建专辑需要登录，请先完成扫码登录")

    # 专辑名称
    if not _fill_by_label(page, ["专辑名称", "专辑名", "名称", "标题"], draft.name):
        raise AlbumCreateError("未找到专辑名称输入框（页面结构可能已变化）")
    _log(f"已填专辑名：{draft.name}")

    # 简介
    if draft.intro:
        if _fill_by_label(page, ["简介", "描述", "介绍"], draft.intro):
            _log("已填专辑简介")
        else:
            _log("⚠ 未找到简介输入框，跳过")

    # 封面
    if draft.cover:
        if _set_cover(page, draft.cover):
            _log(f"已上传封面：{draft.cover.name}")
        else:
            _log("⚠ 未找到封面上传入口，跳过")

    # 分类（尽力而为：点开分类下拉并按文字选）
    if draft.category:
        try:
            sel = page.locator(
                ".ant-select-selector, [class*=select-selector], [class*=category]"
            ).first
            if sel.count() > 0:
                sel.click(timeout=5000)
                page.wait_for_timeout(1000)
                opt = page.get_by_text(draft.category, exact=False).first
                if opt.count() > 0:
                    opt.click(timeout=5000)
                    page.wait_for_timeout(600)
                    _log(f"已选分类：{draft.category}")
        except (PWError, PWTimeout):
            _log("⚠ 分类未能自动选择，请在浏览器中手动补选")

    # 提交
    submitted = False
    for text in ("创建", "确定", "提交", "保存", "完成"):
        try:
            btn = page.get_by_role("button", name=text, exact=False).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=8000)
                submitted = True
                _log(f"已点击提交按钮：{text}")
                break
        except (PWError, PWTimeout):
            continue

    if not submitted:
        raise AlbumCreateError("未找到创建专辑的提交按钮（页面结构可能已变化）")

    # 等待成功反馈
    ok = False
    for _ in range(20):
        page.wait_for_timeout(700)
        txt = page.inner_text("body")
        if any(k in txt for k in ("创建成功", "已创建", "创建完成")):
            ok = True
            break
        if "专辑名称" not in txt and "专辑" in txt:
            ok = True
            break
    if not ok:
        _log("⚠ 未明确检测到成功提示，请检查浏览器窗口")
    else:
        _log("✅ 专辑创建成功")
    return page.url


def album_exists_in_picker(page: Any, album_name: str) -> bool:
    """在上传页的专辑选择器里检查专辑是否存在。"""
    try:
        page.goto(
            "https://www.ximalaya.com/reform-upload/page/webCenter/upload",
            timeout=60_000,
        )
        page.wait_for_timeout(2500)
        btn = page.locator("button.search-select-album-btn-2fDgDdbT").first
        btn.click(timeout=15_000)
        page.wait_for_timeout(1500)
        exact = f"span.album-title-text-4EH5AG-r:text-is('{album_name}')"
        if page.locator(exact).count() > 0:
            return True
        items = page.locator("div.scroll-item-content-252FXLKk").all_inner_texts()
        return any(t.strip() == album_name for t in items)
    except Exception:  # noqa: BLE001
        return False

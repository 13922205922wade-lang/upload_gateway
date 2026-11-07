"""专辑与发布状态校验。

利用创作中心的后台接口（在已登录页面内 fetch，自动携带 cookie）：

  GET /reform-upload/album/choose?page=&pageSize=&kw=
      列出账号里可选的专辑（上传页的专辑下拉用的就是它）
  GET /reform-upload/manage/album/tracks?albumId=&page=&pageSize=&order=ASC&state=1
      列出专辑内已发布的声音
  GET /reform-upload/anchorTrack/edit?trackId=
      单条声音的详情（标题、所属专辑、可见范围）

用途：
- 上传前预检目标专辑是否存在，避免逐章等待超时才发现专辑名写错
- 上传后核对章节数与顺序，确认没有漏传或错序
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ALBUM_CHOOSE_URL = "https://www.ximalaya.com/reform-upload/album/choose"

# 在页面上下文中执行，credentials=include 会带上登录 cookie
_FETCH_JSON_JS = (
    "(u)=>fetch(u,{credentials:'include',"
    "headers:{'Accept':'application/json, text/plain, */*'}})"
    ".then(r=>r.json())"
)


UPLOAD_PAGE_URL = "https://www.ximalaya.com/reform-upload/page/webCenter/upload"


def _ensure_on_site(page: Any) -> None:
    """确保页面在喜马拉雅域名下。

    在 about:blank 上 fetch 同源接口会失败（没有 origin、拿不到 cookie），
    接口会静默返回空 —— 表现为"账号里一个专辑都没有"。
    """
    try:
        origin = page.evaluate("() => location.origin") or ""
    except Exception:  # noqa: BLE001
        origin = ""
    if "ximalaya.com" not in origin:
        try:
            page.goto(UPLOAD_PAGE_URL, timeout=60_000)
            page.wait_for_timeout(3000)
        except Exception:  # noqa: BLE001
            pass


class AlbumApiError(Exception):
    """专辑接口调用失败（把真实原因带出来，别再静默返回空列表）。"""


def list_albums(
    page: Any,
    *,
    keyword: str = "",
    page_size: int = 50,
    attempts: int = 2,
    strict: bool = False,
) -> list[dict[str, Any]]:
    """列出账号里可选的专辑。

    走接口而不是读页面，是因为上传页上的专辑下拉**要等文件上传成功才出现**，
    没法在"还没开始传"的时候查看自己有哪些专辑。

    返回 [{"album_id": int, "title": str, "track_count": int|None}, ...]

    strict=True 时失败会抛 AlbumApiError（带真实原因）；默认 False 时
    仍然返回空列表，但把原因记在 last_error 里供调用方展示 ——
    之前这里是静默吞掉异常，界面上只会显示一句"读不到专辑列表"，
    让人无从下手。
    """
    last_error: str = ""
    for attempt in range(max(1, attempts)):
        try:
            _ensure_on_site(page)
            url = f"{ALBUM_CHOOSE_URL}?page=1&pageSize={page_size}&kw={keyword}"
            r = page.evaluate(_FETCH_JSON_JS, url)
            if not isinstance(r, dict):
                last_error = f"接口返回的不是 JSON 对象：{str(r)[:120]}"
                continue
            ret = r.get("ret")
            data = r.get("data") or {}
            infos = data.get("infos")
            if ret not in (0, 200, None) or infos is None:
                last_error = f"接口报错 ret={ret} msg={r.get('msg') or r.get('message')}"
                continue
            out: list[dict[str, Any]] = []
            for it in infos:
                if not isinstance(it, dict):
                    continue
                title = it.get("title") or ""
                if not title:
                    continue
                extra = it.get("additionalInfo") or {}
                out.append(
                    {
                        "album_id": it.get("albumId"),
                        "title": title,
                        "track_count": extra.get("freeTracks"),
                        "category_id": it.get("categoryId"),
                    }
                )
            list_albums.last_error = ""
            return out
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc).splitlines()[0][:200]
            if attempt + 1 < attempts:
                try:
                    page.wait_for_timeout(1500)
                except Exception:  # noqa: BLE001
                    pass

    list_albums.last_error = last_error  # type: ignore[attr-defined]
    if strict:
        raise AlbumApiError(last_error or "读取专辑列表失败")
    return []


list_albums.last_error = ""  # type: ignore[attr-defined]


def delete_track(page: Any, track_id: int | str) -> bool:
    """删除专辑里的一条声音（不可恢复）。"""
    try:
        r = page.evaluate(
            """(body)=>fetch(
                'https://www.ximalaya.com/reform-upload/manage/album/track/delete',
                {method:'POST', credentials:'include',
                 headers:{'Content-Type':'application/json',
                          'Accept':'application/json, text/plain, */*'},
                 body: JSON.stringify(body)}).then(r=>r.json())""",
            {"trackId": int(track_id)},
        )
        return bool((r or {}).get("ret") == 0)
    except Exception:  # noqa: BLE001
        return False


@dataclass
class TrackInfo:
    """专辑内的一条声音。"""

    track_id: str
    title: str
    visible_crowd_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "title": self.title,
            "visible_crowd_type": self.visible_crowd_type,
        }


class AlbumManager:
    """专辑查询与校验。"""

    def __init__(self, page: Any, *, log=None) -> None:
        self.page = page
        self._log = log or (lambda m: None)

    def _fetch(self, url: str) -> dict[str, Any]:
        return self.page.evaluate(_FETCH_JSON_JS, url)

    def list_tracks(self, album_id: int | str, page_size: int = 10) -> list[TrackInfo]:
        """按平台在线顺序列出专辑内的声音。

        pageSize 用 10 —— 实测这个接口对 pageSize 敏感（传 100 会返回空），
        用 10 逐页翻最稳。
        """
        out: list[TrackInfo] = []
        page_no = 1
        while True:
            url = (
                "https://www.ximalaya.com/reform-upload/manage/album/tracks"
                f"?albumId={album_id}&page={page_no}&pageSize={page_size}"
                "&order=ASC&state=1"
            )
            try:
                r = self._fetch(url)
            except Exception as exc:  # noqa: BLE001
                self._log(f"拉取专辑声音列表失败：{exc}")
                break
            if not isinstance(r, dict) or r.get("ret") != 0:
                self._log(
                    "拉取专辑声音列表失败："
                    f"ret={r.get('ret') if isinstance(r, dict) else '?'}"
                    f" msg={r.get('msg') if isinstance(r, dict) else ''}"
                )
                break
            data = r.get("data") or {}
            items = data.get("tracks") or data.get("infos") or []
            for it in items:
                out.append(
                    TrackInfo(
                        track_id=str(it.get("trackId") or ""),
                        title=it.get("title") or "",
                        visible_crowd_type=str(it.get("visibleCrowdType") or ""),
                    )
                )
            total = data.get("totalSize") or data.get("total") or 0
            if not items:
                break
            if total and page_no * page_size >= total:
                break
            if not total and len(items) < page_size:
                break
            page_no += 1
        return out

    def track_detail(self, track_id: int | str) -> dict[str, Any]:
        """单条声音详情。"""
        url = f"https://www.ximalaya.com/reform-upload/anchorTrack/edit?trackId={track_id}"
        r = self._fetch(url)
        data = (r or {}).get("data") or {}
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------

    def find_album_id(self, album_name: str) -> int | None:
        """在上传页的专辑选择器里找专辑 ID。

        打开选择器后读取列表项上的 albumId 属性（若页面未暴露则返回 None）。
        """
        try:
            self.page.goto(
                "https://www.ximalaya.com/reform-upload/page/webCenter/upload",
                timeout=60_000,
            )
            self.page.wait_for_timeout(2500)
            btn = self.page.locator("button.search-select-album-btn-2fDgDdbT").first
            btn.click(timeout=15_000)
            self.page.wait_for_timeout(1200)
            found = self.page.evaluate(
                """(name) => {
                    const items = Array.from(document.querySelectorAll(
                        'div[class*=scroll-item-content]'));
                    for (const it of items) {
                        const t = (it.innerText || '').trim();
                        if (!t) continue;
                        const holder = it.closest('[albumid]') || it;
                        const id = holder.getAttribute('albumid')
                                || holder.getAttribute('data-album-id')
                                || it.querySelector('[albumid]')?.getAttribute('albumid');
                        if (t === name) return {id: id || null, title: t};
                    }
                    return null;
                }""",
                album_name,
            )
            if found and found.get("id"):
                return int(found["id"])
        except Exception as exc:  # noqa: BLE001
            self._log(f"查找专辑 ID 失败：{exc}")
        return None

    def verify_album_contents(
        self, album_id: int | str, expected_titles: list[str]
    ) -> dict[str, Any]:
        """核对专辑内声音与预期章节标题是否一致。

        比对时忽略空格差异（平台上的标题可能是用户自己填过的，
        全角/半角空格混用很常见）。
        """
        tracks = self.list_tracks(album_id)
        actual = [t.title for t in tracks]
        expected = list(expected_titles)

        def _norm(s: str) -> str:
            return " ".join((s or "").replace("\u3000", " ").split()).lower()

        actual_norm = {_norm(t) for t in actual}
        missing = [t for t in expected if _norm(t) not in actual_norm]
        extra = [t for t in actual if _norm(t) not in {_norm(e) for e in expected}]
        matched = len(expected) - len(missing)

        order_ok = False
        if not missing and not extra and len(actual) == len(expected):
            order_ok = [_norm(t) for t in actual] == [_norm(e) for e in expected]

        return {
            "ok": not missing and not extra and len(actual) == len(expected),
            "count": len(actual),
            "expected_count": len(expected),
            "matched": matched,
            "missing": missing,
            "extra": extra,
            "order_ok": order_ok,
            "tracks": [t.to_dict() for t in tracks],
        }

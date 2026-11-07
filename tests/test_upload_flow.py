"""端到端演练（离线自测，不需要登录喜马拉雅）。

在本地构造一个与**真实上传页同构**的页面，验证 uploader 的表单交互逻辑：

  选文件 / 选专辑 / 填标题 / 设权限 / 挂字幕 / 发布

页面结构严格照抄 2026-10 实测的真实 DOM，包括三个曾经踩过的坑：
  1. 「权限设置」藏在**折叠的「更多设置」面板**里 —— 折叠时那些 radio 是 0×0，
     点它等于点了个看不见的模板，平台真实可见性纹丝不动。
  2. 专辑选择器是精确文本匹配，账号专辑名和套包 title 不同就选不中。
  3. 平台一次只收**一个**字幕文件，双语要用 ` | ` 隔开写进同一个 LRC。

运行：python tests/test_upload_flow.py
"""
import json
import os
import sys
from pathlib import Path

# 用相对路径定位项目根目录，工作区整体搬移后依然可用
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from xmly.package import find_packages, parse_package, summarize  # noqa: E402
from xmly.uploader import (  # noqa: E402
    ChapterResult,
    UploadPlan,
    UploadState,
    XimalayaUploader,
    _match_album,
)


def _find_package() -> str:
    """定位样例套包：优先环境变量，其次常见位置。

    都找不到时**自造一个最小套包**，让测试不依赖外部文件就能跑 ——
    真实套包随时可能被清理掉，测试不该因此挂掉。
    """
    env = os.environ.get("XIMALAYA_PACKAGE")
    if env and Path(env).is_dir():
        return env
    candidates = [
        Path.home() / "Downloads" / "oz01a-ximalaya-zh-CN-r0003",
        PROJECT_ROOT / "packages" / "oz01a-ximalaya-zh-CN-r0003",
    ]
    for c in candidates:
        if c.is_dir():
            return str(c)
    return _make_synthetic_package()


def _make_synthetic_package() -> str:
    """造一个最小可解析的套包（1 章 + 双语 LRC + 一个假 mp3）。"""
    import shutil
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="xmly_test_pkg_"))
    ch = root / "chapters" / "ch01"
    ch.mkdir(parents=True)

    mp3 = ch / "ch01.mp3"
    mp3.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 512)
    lrc = ch / "ch01.lrc"
    lrc.write_text(
        "[00:00.240]第1章 龙卷风\n"
        "[00:00.240]Chapter I. The Cyclone\n"
        "[00:03.934]多萝西住在广阔的堪萨斯大草原中央，\n"
        "[00:03.934]Dorothy lived in the midst of the great Kansas prairies.\n",
        encoding="utf-8",
    )

    manifest = {
        "product_id": "test-oz01",
        "identifier": "urn:test:oz01:r0001",
        "version": "r0001",
        "title": "绿野仙踪 · 喜马拉雅图片有声书 · 普通话",
        "author": "L. Frank Baum",
        "language": "zh-CN",
        "source": {"model": "index-tts-2.5"},
        "schema": "ximalaya-picture-audiobook/v1",
        "chapters": [
            {
                "id": "ch01",
                "title": "第1章　龙卷风",
                "duration_seconds": 432.0,
                "audio": {"path": "chapters/ch01/ch01.mp3", "bytes": mp3.stat().st_size},
                "subtitles": {"path": "chapters/ch01/ch01.srt"},
                "bilingual_lrc": {"path": "chapters/ch01/ch01.lrc"},
                "images": [],
            }
        ],
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"（未找到样例套包，已生成临时套包：{root}）")
    return str(root)


PKG = _find_package()

# --------------------------------------------------------------------------
# 与真实页同构的模拟页
# --------------------------------------------------------------------------

HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<div class="webuploader-pick">上传音频
  <input type="file" id="f" accept="audio/*"
         style="position:absolute;left:-9999px;width:1px;height:1px"></div>
<div id="uploaded"></div>

<div class="ant-form-item">
  <div class="ant-form-item-label"><label>节目标题</label></div>
  <input class="ant-input" placeholder="请输入声音标题">
</div>

<button class="ant-btn search-select-album-btn-2fDgDdbT">添加到专辑</button>
<div id="picker" style="display:none">
  <div class="scroll-item-content-252FXLKk">
    <span class="album-title-text-4EH5AG-r">睡前有声读物：《绿野仙踪》（奥兹国历险记第一集）</span></div>
  <div class="scroll-item-content-252FXLKk">
    <span class="album-title-text-4EH5AG-r">Tobeart 《绒布小兔》有声读物</span></div>
  <div class="scroll-item-content-252FXLKk">不进专辑</div>
  <div class="scroll-item-content-252FXLKk">创建新专辑</div>
</div>

<div class="ant-form-item">
  <div class="ant-form-item-label"><label>字幕</label></div>
  <label class="ant-radio-wrapper">
    <span class="ant-radio"><input type="radio" class="ant-radio-input"
      name="submode" value="0" checked><span class="ant-radio-inner"></span></span>
    <span>字幕文件</span></label>
  <label class="ant-radio-wrapper">
    <span class="ant-radio"><input type="radio" class="ant-radio-input"
      name="submode" value="1"><span class="ant-radio-inner"></span></span>
    <span>编辑字幕</span></label>
  <div class="upload_lrc_btn-1hFoeJaA">上传字幕文件
    <input type="file" id="lrcInput" accept=".lrc"
           style="position:absolute;left:-9999px;width:1px;height:1px"></div>
  <div class="gen-lrc-box" style="position:relative;display:none">
    <textarea class="ant-input" style="width:480px;height:120px"></textarea>
    <button aria-label="确认提交字幕" type="button" class="ant-btn ant-btn-primary">
      <span>确认提交</span></button>
  </div>
</div>

<div class="ant-form-item">
  <div class="ant-form-item-label"><label>是否AI合成</label></div>
  <label class="ant-radio-wrapper">
    <span class="ant-radio"><input type="radio" class="ant-radio-input"
      name="ai" value="true"><span class="ant-radio-inner"></span></span>
    <span>是</span></label>
  <label class="ant-radio-wrapper">
    <span class="ant-radio"><input type="radio" class="ant-radio-input"
      name="ai" value="false" checked><span class="ant-radio-inner"></span></span>
    <span>否</span></label>
</div>

<div id="moreToggle" style="cursor:pointer">更多设置</div>
<div id="morePanel" style="display:none">
  <div class="ant-form-item">
    <div class="ant-form-item-label"><label>权限设置</label></div>
    <label class="track-permission-switch-radio ant-radio-wrapper">
      <span class="ant-radio"><input type="radio" class="ant-radio-input"
        name="vis" value="2" checked><span class="ant-radio-inner"></span></span>
      <span>公开所有人可见</span></label>
    <label class="track-permission-switch-radio ant-radio-wrapper">
      <span class="ant-radio"><input type="radio" class="ant-radio-input"
        name="vis" value="3"><span class="ant-radio-inner"></span></span>
      <span>仅好友可见只有相互关注可见</span></label>
    <label class="track-permission-switch-radio ant-radio-wrapper">
      <span class="ant-radio"><input type="radio" class="ant-radio-input"
        name="vis" value="1"><span class="ant-radio-inner"></span></span>
      <span>私密仅自己可见</span></label>
  </div>
</div>

<textarea id="soundRichIntro" style="width:480px;height:60px"></textarea>
<button class="ant-btn publish-box-new-btn-1I1nky3y confirm-publish">确认发布</button>
<div id="status"></div>

<script>
document.querySelector('.webuploader-pick').addEventListener('click', (ev)=>{
  if (ev.target.id !== 'f') { ev.preventDefault(); document.getElementById('f').click(); }
});
document.getElementById('f').addEventListener('change', e=>{
  const n = e.target.files[0] ? e.target.files[0].name : '';
  document.getElementById('uploaded').innerText = n + ' 上传成功';
});
// 专辑下拉
document.querySelector('button.search-select-album-btn-2fDgDdbT').addEventListener('click', ()=>{
  document.getElementById('picker').style.display='block';
});
document.querySelectorAll('#picker span.album-title-text-4EH5AG-r').forEach(el=>{
  el.addEventListener('click', ()=>{
    document.getElementById('picker').style.display='none';
    document.querySelector('button.search-select-album-btn-2fDgDdbT').innerText = el.innerText;
  });
});
// 字幕：切到「编辑字幕」才出现 gen-lrc-box；「确认提交」会走一次上传
window.__lrcSubmitted = false;
document.querySelectorAll('.ant-form-item .ant-radio-wrapper').forEach(lab=>{
  const inp = lab.querySelector('input.ant-radio-input');
  if (!inp || inp.name !== 'submode') return;
  lab.addEventListener('click', ()=>{
    document.querySelectorAll('input.ant-radio-input[name=submode]').forEach(i=>{
      i.checked=false; const w=i.closest('.ant-radio-wrapper');
      if(w) w.querySelector('.ant-radio').classList.remove('ant-radio-checked');
    });
    inp.checked=true;
    lab.querySelector('.ant-radio').classList.add('ant-radio-checked');
    const box=document.querySelector('div.gen-lrc-box');
    if (box) box.style.display = (inp.value==='1') ? 'block' : 'none';
  });
});
document.querySelector('button[aria-label="确认提交字幕"]').addEventListener('click', ()=>{
  window.__lrcSubmitted = true;
  // 平台把字幕转成 lrc.json 传到对象存储
  try {
    fetch('https://cupload.ximalaya.com/upload/file/blk?name=lrc.json', {method:'POST'})
      .catch(()=>{});
  } catch(e) {}
});
// 字幕：上传文件后把内容读进 textarea（老路径，平台仍保留）
document.querySelector('div.upload_lrc_btn-1hFoeJaA').addEventListener('click', (ev)=>{
  if (ev.target.id !== 'lrcInput') { ev.preventDefault(); document.getElementById('lrcInput').click(); }
});
document.getElementById('lrcInput').addEventListener('change', e=>{
  const f = e.target.files[0]; if (!f) return;
  const rd = new FileReader();
  rd.onload = () => {
    const ta=document.querySelector('div.gen-lrc-box textarea');
    if (ta) ta.value = rd.result;
  };
  rd.readAsText(f);
});
// 「更多设置」折叠面板
document.getElementById('moreToggle').addEventListener('click', ()=>{
  document.getElementById('morePanel').style.display='block';
});
// 「是否AI合成」radio 的选中态
document.querySelectorAll('.ant-radio-wrapper').forEach(lab=>{
  const inp = lab.querySelector('input.ant-radio-input');
  if (!inp || inp.name !== 'ai') return;
  lab.addEventListener('click', ()=>{
    document.querySelectorAll('input.ant-radio-input[name=ai]').forEach(i=>{
      i.checked=false; const w=i.closest('.ant-radio-wrapper');
      if(w) w.querySelector('.ant-radio').classList.remove('ant-radio-checked');
    });
    inp.checked=true;
    lab.querySelector('.ant-radio').classList.add('ant-radio-checked');
  });
});
// 权限 radio 的选中态（antd 把 ant-radio-checked 打在 .ant-radio 上）
document.querySelectorAll('label.track-permission-switch-radio').forEach(lab=>{
  lab.addEventListener('click', ()=>{
    document.querySelectorAll('label.track-permission-switch-radio').forEach(l=>{
      l.querySelector('.ant-radio').classList.remove('ant-radio-checked');
      l.querySelector('input.ant-radio-input').checked = false;
    });
    const inp = lab.querySelector('input.ant-radio-input');
    inp.checked = true;
    lab.querySelector('.ant-radio').classList.add('ant-radio-checked');
  });
});
window.KindEditor = { instances: {'0': { html:(h)=>{
  document.getElementById('soundRichIntro').value=h.replace(/<br>/g,'\\n'); },
  sync:()=>{} }}, sync:(id)=>{} };
document.querySelector('button.confirm-publish').addEventListener('click', ()=>{
  document.getElementById('status').innerText = '发布成功';
  history.pushState({}, '', '/uploadSuccess');
  const titleEl=document.querySelector("input[placeholder='请输入声音标题']");
  try {
    fetch('https://www.ximalaya.com/reform-upload/upload/create', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        albumId: 999000001, visibleCrowdType: 1, isAiTrack: true,  // 测试夹具：虚构 ID
        soundTitle: titleEl ? titleEl.value : '',
        lrcPath: window.__lrcSubmitted
          ? 'http://fdfs.xmcdn.com/storages/test/lrc.json' : ''
      })
    }).catch(()=>{});
  } catch(e) {}
});
</script></body></html>"""

# 模拟"人工点了确认发布"：等标题填好后再点。
# 直接写进 HTML 而不是 evaluate —— 上传流程会 goto 换页，evaluate 里的定时器会被冲掉。
_AUTO_CLICK_JS = """<script>
(function(){
  const iv = setInterval(()=>{
    const t = document.querySelector("input[placeholder='请输入声音标题']");
    const b = document.querySelector('button.confirm-publish');
    if (t && t.value && b) { clearInterval(iv); b.click(); }
  }, 200);
  setTimeout(()=>clearInterval(iv), 40000);
})();
</script>"""


def make_route(autopublish: bool = False):
    def route(r):
        body = HTML.replace("</body>", _AUTO_CLICK_JS + "</body>") if autopublish else HTML
        r.fulfill(status=200, content_type="text/html", body=body)

    return route


def main() -> int:
    pkg = parse_package(PKG)
    ch = pkg.chapters[0]

    checks: list[tuple[str, bool, str]] = []

    # ---- 纯逻辑：专辑模糊匹配 ----
    cands = [
        "睡前有声读物：《绿野仙踪》（奥兹国历险记第一集）",
        "Tobeart 《绒布小兔》有声读物",
    ]
    matched, reason, cands2 = _match_album(pkg.title, cands)
    checks.append(
        (
            "专辑模糊匹配",
            matched == cands[0],
            f"套包《{pkg.title}》→ 《{matched}》（{reason}）",
        )
    )
    no_match, _r, _c = _match_album("完全不相干的东西", cands)
    checks.append(("无关专辑名不误配", no_match is None, f"结果={no_match}"))

    # 并列候选必须报歧义，不能静默选一个（否则会传到错误专辑）
    dup = [
        "Tobeart有声书 | 绿野仙踪｜剧场版｜全集",
        "睡前有声读物：《绿野仙踪》（奥兹国历险记第一集）",
        "Tobeart 《绒布小兔》有声读物",
    ]
    m2, r2, c2 = _match_album(pkg.title, dup)
    checks.append(
        (
            "多个专辑同样匹配时报歧义（不猜）",
            m2 is None and len(c2) == 2,
            f"结果={m2} 候选={c2} 理由={r2}",
        )
    )
    exact, r3, _c3 = _match_album(dup[1], dup)
    checks.append(
        ("精确同名直接命中", exact == dup[1], f"→ {exact}（{r3}）")
    )

    # ---- 纯逻辑：套包版本 / 指纹 / 断点续传 ----
    checks.append(
        (
            "套包版本与身份可读",
            bool(pkg.package_key) and bool(pkg.fingerprint),
            f"{pkg.package_key} / {pkg.fingerprint}",
        )
    )
    checks.append(
        (
            "章节内容指纹稳定",
            pkg.chapters[0].fingerprint == parse_package(PKG).chapters[0].fingerprint,
            pkg.chapters[0].fingerprint,
        )
    )

    cid = pkg.chapters[0].id

    def _mk_state(package_key: str, fingerprint: str) -> UploadState:
        st = UploadState(album_name=pkg.title, package_key=package_key)
        st.done[cid] = ChapterResult(
            chapter_id=cid,
            title=pkg.chapters[0].title,
            ok=True,
            published=True,
            message="发布成功",
            fingerprint=fingerprint,
        )
        return st

    plan0 = UploadPlan(album_name=pkg.title, subtitle_mode="lrc")

    def _todo(state: UploadState, existing: set[str] | None = None) -> list[str]:
        """跑一次空批次，返回实际会传的章节 id（其余都被跳过）。"""
        captured: list[str] = []
        up = XimalayaUploader(page=None, log=lambda m: None)

        def fake(chapter, plan, *, index=1, total=1):
            captured.append(chapter.id)
            return ChapterResult(
                chapter_id=chapter.id, title=chapter.title, ok=True, published=True
            )

        up.upload_chapter = fake  # type: ignore[method-assign]
        up.run_batch(pkg, plan0, state=state, resume=True, existing_titles=existing)
        return captured

    # 断点续传：套包换了版本 → 旧记录不再作为跳过依据
    st_old = _mk_state("urn:old:r0001@R1", "deadbeefdeadbeef")
    st_same = _mk_state(pkg.package_key, pkg.chapters[0].fingerprint)
    st_stale = _mk_state(pkg.package_key, "0000000000000000")  # 内容变了
    st_legacy = UploadState(album_name=pkg.title)  # 旧格式：无版本无指纹
    st_legacy.done[cid] = ChapterResult(
        chapter_id=cid, title=pkg.chapters[0].title, ok=True, published=True
    )

    checks.append(
        (
            "套包换版本 → 不再跳过（旧状态作废）",
            _todo(st_old) == [cid],
            f"要传={_todo(st_old)}",
        )
    )
    checks.append(
        ("同版本同内容 → 跳过", _todo(st_same) == [], f"要传={_todo(st_same)}")
    )
    checks.append(
        (
            "同版本但内容变了 → 不跳过",
            _todo(st_stale) == [cid],
            f"要传={_todo(st_stale)}",
        )
    )
    checks.append(
        (
            "旧格式状态（无版本无指纹）→ 不盲目跳过",
            _todo(st_legacy) == [cid],
            f"要传={_todo(st_legacy)}",
        )
    )
    checks.append(
        (
            "专辑里已有同名章节 → 跳过",
            _todo(st_same, existing={pkg.chapters[0].title}) == [],
            "按平台实况跳过",
        )
    )
    checks.append(
        (
            "标题全角/半角空格视为同一章",
            _todo(st_same, existing={pkg.chapters[0].title.replace("\u3000", " ")})
            == [],
            "归一化后命中",
        )
    )

    pkgs = find_packages()
    checks.append(
        (
            "能自动发现材料目录里的套包",
            len(pkgs) > 0,
            f"{len(pkgs)} 个，最新={pkgs[0].name if pkgs else '-'}",
        )
    )

    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        ctx = b.new_context()
        page = ctx.new_page()

        def route(r):
            r.fulfill(status=200, content_type="text/html", body=HTML)

        page.route("**/reform-upload/**", route)

        up = XimalayaUploader(page, log=lambda m: print("  LOG:", m))
        plan = UploadPlan(
            album_name=pkg.title,
            visibility="private",
            subtitle_mode="lrc",
            publish=True,
        )
        res = up.upload_chapter(ch, plan, index=1, total=1)

        print(f"\nRESULT: {res.ok} | {res.message}")
        checks.append(("章节上传成功", res.ok, res.message))
        checks.append(("标记为已发布", res.published, f"published={res.published}"))

        # 标题
        title = page.locator("input.ant-input").input_value()
        checks.append(("标题已填", title == ch.title, f"title={title!r}"))

        # 专辑
        album_now = page.evaluate(
            "()=>document.querySelector(\"button[class*='search-select-album-btn']\").innerText.trim()"
        )
        checks.append(
            (
                "归属到正确专辑",
                album_now == "睡前有声读物：《绿野仙踪》（奥兹国历险记第一集）",
                f"album={album_now!r}",
            )
        )

        # 权限：必须是「私密仅自己可见」（value=1），不是平台默认的公开
        vis = page.evaluate(
            """()=>{
                const items = Array.from(document.querySelectorAll('.ant-form-item')).filter(it=>{
                    const l = it.querySelector('.ant-form-item-label');
                    return l && /权限设置/.test(l.innerText);
                });
                if(!items.length) return {err:'no item'};
                const cur = Array.from(items[0].querySelectorAll('input.ant-radio-input')).find(r=>{
                    const w=r.closest('.ant-radio-wrapper'); const d=w?w.querySelector('.ant-radio'):null;
                    return d? d.classList.contains('ant-radio-checked') : r.checked;
                });
                const lab = cur? cur.closest('label.ant-radio-wrapper') : null;
                return {value: cur?cur.value:null, label: lab?lab.innerText.trim():null};
            }"""
        )
        checks.append(
            (
                "可见性=私密（不是平台默认的公开）",
                vis.get("value") == "1",
                f"vis={vis}",
            )
        )

        # 字幕：应该是合并后的双语 LRC，含 " | " 分隔
        lrc = page.evaluate(
            "()=>{const t=document.querySelector('div.gen-lrc-box textarea');"
            "return t? t.value:'';}"
        )
        checks.append(
            (
                "字幕已挂载",
                len(lrc) > 100,
                f"{len(lrc)} 字符",
            )
        )
        checks.append(
            ("双语用 ' | ' 合并成单个文件", " | " in lrc, "含分隔符" if " | " in lrc else "缺分隔符")
        )
        checks.append(
            ("字幕含 LRC 时间戳", "[00:" in lrc, lrc.splitlines()[0][:60] if lrc else "(空)")
        )

        # 发布
        checks.append(
            ("发布后跳转 uploadSuccess", "uploadSuccess" in page.url, f"url={page.url}")
        )

        b.close()

    # ---- 人工确认模式：平台没有草稿箱，必须等真发布 ----
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        ctx = b.new_context()
        page = ctx.new_page()

        def route(r):
            r.fulfill(status=200, content_type="text/html", body=HTML)

        page.route("**/reform-upload/**", route)

        # 模拟人工：标题一填好就自己点「确认发布」
        page.route("**/reform-upload/**", make_route(autopublish=True))

        up = XimalayaUploader(page, log=lambda m: None)
        plan = UploadPlan(
            album_name=pkg.title,
            visibility="private",
            subtitle_mode="lrc",
            publish=False,  # 人工确认模式
            manual_timeout=20,
        )
        res = up.upload_chapter(ch, plan, index=1, total=1)
        checks.append(
            (
                "人工确认模式：等人工点了才算发布",
                res.ok and res.published,
                f"ok={res.ok} published={res.published} msg={res.message}",
            )
        )

        # 再来一次：没人点 → 必须报失败，不能假装成功
        page2 = ctx.new_page()
        page2.route("**/reform-upload/**", route)
        up2 = XimalayaUploader(page2, log=lambda m: None)
        plan2 = UploadPlan(
            album_name=pkg.title,
            visibility="private",
            subtitle_mode="lrc",
            publish=False,
            manual_timeout=4,
        )
        res2 = up2.upload_chapter(ch, plan2, index=1, total=1)
        checks.append(
            (
                "没人点确认 → 明确报失败（不会静默丢章）",
                (not res2.ok) and "没有保存到喜马拉雅" in res2.message,
                f"ok={res2.ok} msg={res2.message}",
            )
        )

        b.close()

    print("\n" + "=" * 62)
    failed = 0
    for name, ok, detail in checks:
        flag = "✅" if ok else "❌"
        if not ok:
            failed += 1
        print(f"  {flag} {name:34s} {detail}")
    print("=" * 62)
    print(f"{'全部通过' if not failed else f'{failed} 项失败'}（共 {len(checks)} 项）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

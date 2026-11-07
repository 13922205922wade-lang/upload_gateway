"""把 24 章从「私密」改成「公开所有人可见」。

走的是平台自己的编辑页路径（已用 ch24 验证可行）：
  编辑页 → 点「公开所有人可见」→ 点「保 存」→ 回读 visibleCrowdType 确认

审核中的节目改可见性不影响审核状态（trackStatus 保持 0），
所以现在就改是安全的：审核没通过之前对外依然看不到。
"""
import sys, json, time
sys.path.insert(0, ".")
from pathlib import Path
from xmly.browser import BrowserSession

PROFILE = "profiles/chrome"
FETCH = "(u)=>fetch(u,{credentials:'include',headers:{'Accept':'application/json'}}).then(r=>r.json())"
LOG = Path("logs") / f"flip-public-{time.strftime('%Y%m%d-%H%M%S')}.log"

log_fp = LOG.open("a", encoding="utf-8")


def log(m: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {m}"
    print(line, flush=True)
    log_fp.write(line + "\n")
    log_fp.flush()


with BrowserSession(PROFILE, headless=True, log=lambda m: None) as s:
    s.launch()
    pg = s.page
    pg.goto("https://www.ximalaya.com/reform-upload/page/webCenter/upload", timeout=60_000)
    pg.wait_for_timeout(4000)

    # 收集专辑内全部 trackId
    ids = []
    for page in range(1, 6):
        u = ("https://www.ximalaya.com/reform-upload/anchorWork/track/list"
             f"?page={page}&keyword=&pageSize=10")
        d = (pg.evaluate(FETCH, u).get("data") or {})
        infos = d.get("infos") or []
        ids += [(i.get("trackId"), i.get("title"), i.get("visibleCrowdType")) for i in infos]
        if not infos:
            break
    log(f"专辑内共 {len(ids)} 条")
    todo = [(tid, title) for tid, title, vis in ids if vis != 2]
    log(f"需要改为公开的 {len(todo)} 条（其余已是公开）")

    ok = fail = 0
    for n, (tid, title) in enumerate(todo, 1):
        try:
            pg.goto(f"https://www.ximalaya.com/reform-upload/page/sound/edit/{tid}",
                    timeout=45_000)
            pg.wait_for_timeout(3500)
            r = pg.evaluate("""()=>{
                const items=Array.from(document.querySelectorAll('.ant-form-item')).filter(it=>{
                    const l=it.querySelector('.ant-form-item-label');
                    return l && /权限设置/.test(l.innerText);});
                if(!items.length) return {err:'no 权限设置'};
                const labs=Array.from(items[0].querySelectorAll('label.ant-radio-wrapper'));
                const lab=labs.find(l=>(l.innerText||'').trim().startsWith('公开'));
                if(!lab) return {err:'no 公开 radio'};
                lab.click();
                return {ok:true};
            }""")
            if not r.get("ok"):
                log(f"  {n}/{len(todo)} {title} → {r.get('err')}")
                fail += 1
                continue
            pg.wait_for_timeout(1000)
            pg.locator("button.ant-btn-primary:has-text('保 存')").first.click(timeout=15_000)
            pg.wait_for_timeout(4500)
            # 回读确认
            pg.goto("https://www.ximalaya.com/reform-upload/page/webCenter/upload",
                    timeout=45_000)
            pg.wait_for_timeout(1200)
            rr = pg.evaluate(FETCH, f"https://www.ximalaya.com/reform-upload/anchorTrack/edit?trackId={tid}")
            ti = (rr.get("data") or {}).get("trackInfo") or {}
            vis = ti.get("visibleCrowdType")
            st = (ti.get("trackStatusInfo") or {}).get("trackStatus")
            if vis == 2:
                log(f"  {n}/{len(todo)} ✅ {title} → 公开(vis=2)，审核状态={st}")
                ok += 1
            else:
                log(f"  {n}/{len(todo)} ❌ {title} → vis={vis}")
                fail += 1
        except Exception as exc:  # noqa: BLE001
            log(f"  {n}/{len(todo)} ❌ {title} → {type(exc).__name__} {str(exc)[:90]}")
            fail += 1

    log(f"—— 改完：成功 {ok}，失败 {fail} ——")
log_fp.close()

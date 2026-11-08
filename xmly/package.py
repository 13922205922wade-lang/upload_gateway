"""喜马拉雅有声书套包解析。

负责把制作端产出的交付套包目录（例如 oz01a-ximalaya-zh-CN-r0003）解析成
结构化的上传计划：章节列表、音频资产、字幕轨、插图资产。

套包约定（见 manifest.json 的 schema 字段）:

1) ximalaya-picture-audiobook/v1 —— 制作端交付包
  manifest.json
  chapters/<chNN>/<chNN>.mp3   音频
  chapters/<chNN>/<chNN>.srt   字幕（中英单语时间轴）
  chapters/<chNN>/<chNN>.lrc   双语时间轨（同时间戳中文行 + 英文行）
  chapters/<chNN>/images/*.jpg 插图

2) oz-reviewed-category-zip/v1 —— 审核分类包（目录或 .zip）
  manifest.json 里只有一个 files[] 清单：
      {chapter, archive_path, source, sha256, bytes}
  chNN/<zh|en>.mp3 + chNN/<zh|en>.lrc，没有标题 / 时长 / 插图。
  解析时把 files[] 归一化成 1) 的 chapters[]，并从就近的旧交付包或 LRC
  里把章节标题、时长、AI 合成标记补回来（见 _normalize_reviewed_manifest）。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from .paths import data_dir

SCHEMA_PREFIX = "ximalaya-picture-audiobook"
REVIEWED_SCHEMA_PREFIX = "oz-reviewed-category-zip"
SUPPORTED_SCHEMAS = (SCHEMA_PREFIX, REVIEWED_SCHEMA_PREFIX)

# LRC 时间戳：[mm:ss.xx] 或 [mm:ss.xxx] 或 [hh:mm:ss.xx]
_LRC_TIME = re.compile(r"^\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\](.*)$")
# 审核包里可能出现三段式 [hh:mm:ss.mmm]，扫描标题/时长时用
_LRC_LINE = re.compile(
    r"^\[(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]\s*(.*)$"
)
_CHAPTER_TITLE_RE = re.compile(r"^第\s*\d+\s*章[\s　]*\S")
_FRONT_MATTER = ("欢迎", "奥兹系列", "有声书", "请", "搜索", "本书", "系列")
# 中日韩统一表意文字（含扩展A）
_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
# 拉丁字母
_LATIN = re.compile(r"[A-Za-z]")


class PackageError(Exception):
    """套包结构不合法。"""


@dataclass
class Cue:
    """一条字幕时间轴。"""

    start_ms: int
    end_ms: int
    zh: str = ""
    en: str = ""


@dataclass
class SubtitleTrack:
    """一条可上传的字幕轨。"""

    lang: str  # zh-CN / en-US
    cues: list[Cue] = field(default_factory=list)

    def to_lrc(self) -> str:
        """导出为标准 LRC（喜马拉雅可识别的单语字幕格式）。"""
        lines = []
        for cue in self.cues:
            text = cue.zh if self.lang.startswith("zh") else cue.en
            if not text:
                continue
            lines.append(f"[{_fmt_ts(cue.start_ms)}]{text}")
        return "\n".join(lines) + "\n"

    def to_srt(self) -> str:
        """导出为 SRT，供平台只接受 SRT 时回退。"""
        out = []
        for i, cue in enumerate(self.cues, 1):
            text = cue.zh if self.lang.startswith("zh") else cue.en
            if not text:
                continue
            out.append(f"{i}\n{_fmt_srt(cue.start_ms)} --> {_fmt_srt(cue.end_ms)}\n{text}\n")
        return "\n".join(out)


@dataclass
class ImageAsset:
    """一张插图。"""

    path: Path
    image_id: str
    start_ms: int | None = None  # 图片在音频时间轴上的起点
    sha256: str | None = None


@dataclass
class Chapter:
    """一章的完整素材。"""

    id: str
    title: str
    duration_seconds: float
    audio: Path
    audio_bytes: int = 0
    audio_sha256: str | None = None
    srt: Path | None = None
    lrc: Path | None = None
    images: list[ImageAsset] = field(default_factory=list)
    index: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and self.audio.exists()

    @property
    def fingerprint(self) -> str:
        """这一章的**内容指纹**。

        套包更新（重新配音、重新出图）之后章节 id 和标题往往不变，
        只有内容变了。断点续传必须靠指纹判断"这一章是不是已经传过"，
        否则换了新套包会被整批跳过 —— 表现就是"跑完了却一章都没传"。
        """
        parts = [self.id, self.title, f"{self.duration_seconds:.3f}"]
        if self.audio_sha256:
            parts.append(self.audio_sha256)
        else:
            try:
                parts.append(str(self.audio.stat().st_size))
            except OSError:
                parts.append("missing")
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


@dataclass
class Package:
    """一个完整套包。"""

    root: Path
    manifest: dict
    chapters: list[Chapter]
    manifest_sha256: str = ""
    origin: Path | None = None  # 由 .zip 导入时，记着原始 zip 路径

    @property
    def title(self) -> str:
        return self.manifest.get("title") or self.root.name

    @property
    def author(self) -> str:
        return self.manifest.get("author") or ""

    @property
    def language(self) -> str:
        return self.manifest.get("language") or "zh-CN"

    @property
    def product_id(self) -> str:
        return self.manifest.get("product_id") or ""

    @property
    def identifier(self) -> str:
        return self.manifest.get("identifier") or ""

    @property
    def version(self) -> str:
        """套包版本号（如 R4 / r0004）。"""
        return str(self.manifest.get("version") or "")

    @property
    def package_key(self) -> str:
        """套包的身份标识：identifier + 版本。

        断点续传用它判断"这份状态是不是当前这个套包的"。
        """
        ident = self.identifier or self.product_id or self.root.name
        return f"{ident}@{self.version}" if self.version else str(ident)

    @property
    def fingerprint(self) -> str:
        """整个套包的内容指纹（manifest + 各章指纹）。"""
        h = hashlib.sha256()
        h.update((self.manifest_sha256 or "").encode("utf-8"))
        for ch in self.chapters:
            h.update(ch.fingerprint.encode("utf-8"))
        return h.hexdigest()[:16]

    @property
    def is_ai_synthesized(self) -> bool:
        """音频是否由 TTS / 语音合成模型生成。

        平台上传页有「是否AI合成」这一项，默认为「否」。套包的 source 里
        记着合成模型（如 index-tts-2.5），据此判断，避免把合成音频
        误报成真人录音。
        """
        source = self.manifest.get("source") or {}
        if not isinstance(source, dict):
            return False
        for key in ("model", "tts_model", "voice_model", "engine"):
            value = str(source.get(key) or "")
            if value and ("tts" in value.lower() or "synth" in value.lower()):
                return True
        return False

    @property
    def total_duration(self) -> float:
        return sum(c.duration_seconds for c in self.chapters)

    @property
    def total_bytes(self) -> int:
        return sum(c.audio_bytes for c in self.chapters)

    @property
    def errors(self) -> list[str]:
        out: list[str] = []
        for ch in self.chapters:
            out.extend(f"{ch.id}: {e}" for e in ch.errors)
        return out


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


def _fmt_ts(ms: int) -> str:
    """毫秒 -> LRC 时间戳 mm:ss.xx"""
    total_cs = round(ms / 10)
    m, rem = divmod(total_cs, 6000)
    s, cs = divmod(rem, 100)
    return f"{m:02d}:{s:02d}.{cs:02d}"


def _fmt_srt(ms: int) -> str:
    """毫秒 -> SRT 时间戳 hh:mm:ss,mmm"""
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _detect_lang(text: str) -> str:
    """粗判一行文本的语言。"""
    if _CJK.search(text):
        return "zh"
    if _LATIN.search(text):
        return "en"
    return "zh" if not text.strip() else "zh"


def parse_bilingual_lrc(path: Path) -> tuple[SubtitleTrack, SubtitleTrack]:
    """拆分双语 LRC。

    套包里的 lrc 是"同时间戳成对"的格式：中文行与英文行共享同一时间戳，
    偶有单独的标题行。拆成 zh-CN / en-US 两条独立字幕轨。
    """
    zh = SubtitleTrack("zh-CN")
    en = SubtitleTrack("en-US")

    rows: list[tuple[int, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        m = _LRC_TIME.match(raw.strip())
        if not m:
            continue
        mm, ss, frac, text = m.groups()
        ms = int(mm) * 60000 + int(ss) * 1000
        if frac:
            ms += int(frac.ljust(3, "0"))
        rows.append((ms, text.strip()))

    # 合并同时间戳的连续行（最多两行：一中一英）
    grouped: dict[int, list[str]] = {}
    for ms, text in rows:
        grouped.setdefault(ms, []).append(text)

    ordered = sorted(grouped.items())
    for i, (ms, texts) in enumerate(ordered):
        end_ms = ordered[i + 1][0] if i + 1 < len(ordered) else ms + 4000
        texts = [t for t in texts if t]
        if not texts:
            continue

        if len(texts) == 1:
            only = texts[0]
            lang = _detect_lang(only)
            cue = Cue(start_ms=ms, end_ms=end_ms)
            setattr(cue, lang, only)
        else:
            # 两行：按语言特征分配，优先保持 中文在前
            a, b = texts[0], texts[1]
            if _detect_lang(a) == "zh" or _detect_lang(b) == "en":
                cue = Cue(start_ms=ms, end_ms=end_ms, zh=a, en=b)
            else:
                cue = Cue(start_ms=ms, end_ms=end_ms, zh=b, en=a)
        zh.cues.append(cue)
        en.cues.append(cue)

    return zh, en


def _safe_rel(rel: str) -> str:
    """校验 manifest / ZIP 里的相对路径，挡住 ../ 与绝对路径。"""
    rel = str(rel).strip().replace("\\", "/")
    p = Path(rel)
    if not rel or p.is_absolute() or rel.startswith("/") or ".." in p.parts:
        raise PackageError(f"套包里有不安全的路径：{rel}")
    return rel


def _zip_has_manifest(path: Path) -> bool:
    """自动发现时判断一个 .zip 是不是套包（只读目录表，不解压）。"""
    try:
        with zipfile.ZipFile(path) as zf:
            return any(
                Path(n).name == "manifest.json"
                and n.count("/") <= 1
                and not n.startswith("__MACOSX")
                for n in zf.namelist()
            )
    except (zipfile.BadZipFile, OSError):
        return False


def _expand_zip(zip_path: Path) -> Path:
    """把审核包 ZIP 展开到运行期缓存；manifest 不变就复用，不重复解压。

    上传需要真实的本地音频路径，所以 ZIP 必须落盘。缓存放在运行期数据目录
    （打包版是 ~/Library/Application Support/...），不进 bundle、不进仓库。
    """
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise PackageError(f"不是有效的套包 ZIP：{exc}") from exc

    with zf:
        names = {n for n in zf.namelist() if not n.endswith("/")}
        manifest_name = next(
            (
                n
                for n in sorted(names)
                if Path(n).name == "manifest.json"
                and n.count("/") <= 1
                and not n.startswith("__MACOSX")
            ),
            None,
        )
        if manifest_name is None:
            raise PackageError(f"ZIP 里没有 manifest.json：{zip_path.name}")
        if ".." in Path(manifest_name).parts or manifest_name.startswith("/"):
            raise PackageError(f"ZIP 里 manifest 路径不安全：{manifest_name}")
        prefix = manifest_name[: -len("manifest.json")]
        raw = zf.read(manifest_name)
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PackageError(f"ZIP 内 manifest.json 解析失败：{exc}") from exc

        rels = [
            _safe_rel(f.get("archive_path") or "")
            for f in (manifest.get("files") or [])
            if isinstance(f, dict)
        ]
        needed = set(rels)
        if "README.txt" in names:
            needed.add("README.txt")

        tag = hashlib.sha256(raw).hexdigest()[:12]
        dest = data_dir() / "expanded_packages" / f"{zip_path.stem}-{tag}"
        expected = [dest / "manifest.json", *(dest / r for r in rels)]
        if all(p.is_file() for p in expected):
            return dest

        dest.mkdir(parents=True, exist_ok=True)
        for rel in sorted(needed):
            src_name = prefix + rel
            if src_name not in names:
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(src_name) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
        (dest / "manifest.json").write_bytes(raw)

        missing = [str(p) for p in expected if not p.is_file()]
        if missing:
            raise PackageError(f"ZIP 解包后缺少文件：{', '.join(missing[:5])}")
    return dest


def _reviewed_language(manifest: dict, rels: list[str]) -> str:
    """审核包没有 language 字段：从 category / 文件名推断。"""
    category = str(manifest.get("category") or "").lower()
    if "zh" in category:
        return "zh-CN"
    if "en" in category:
        return "en-US"
    for rel in rels:
        stem = Path(rel).stem.lower()
        if stem in ("zh", "zh-cn", "zh-hans"):
            return "zh-CN"
        if stem in ("en", "en-us"):
            return "en-US"
    return "zh-CN"


def _lrc_scan(path: Path | None) -> tuple[str, float]:
    """从审核包 LRC 里捞章节标题 + 近似时长（最后一个时间戳）。"""
    if not path or not path.is_file():
        return "", 0.0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "", 0.0

    title = ""
    short: list[str] = []
    max_ms = 0
    for i, raw in enumerate(lines):
        m = _LRC_LINE.match(raw.strip())
        if not m:
            continue
        hours, minutes, seconds, frac, text = m.groups()
        ms = (
            int(hours or 0) * 3600000
            + int(minutes) * 60000
            + int(seconds) * 1000
            + int((frac or "0").ljust(3, "0"))
        )
        max_ms = max(max_ms, ms)
        text = text.strip()
        if i >= 40 or not text:
            continue
        if _CHAPTER_TITLE_RE.match(text) and not title:
            title = text
        elif (
            2 <= len(text) <= 16
            and not any(c in text for c in "，。,.!?；;：:")
            and not any(w in text for w in _FRONT_MATTER)
        ):
            short.append(text)
    return title or (short[0] if short else ""), round(max_ms / 1000, 3)


def _sibling_metadata(root: Path, origin: Path | None, manifest: dict) -> dict:
    """审核包没有书名 / 章节标题 / AI 标记，就近找同一本书的旧交付包借。

    审核包的 source 字段指向 dist/.../r0004/...，材料目录里通常就摆着那份
    交付包；借它的标准章节标题能保证「平台已有同名章节」的去重逻辑仍然有效。
    找不到也不报错 —— LRC 启发式与兜底标题会接上。
    """
    category = str(manifest.get("category") or "").lower()
    hint = "en" if ("en" in category and "zh" not in category) else "zh"
    bases: list[Path] = []
    for base in (
        origin.parent if origin else None,
        root.parent,
        root.parent.parent,
    ):
        if base and base.is_dir() and base not in bases:
            bases.append(base)

    candidates: list[tuple[int, dict]] = []
    for base in bases:
        files = [base / "manifest.json", *sorted(base.glob("*/manifest.json"))]
        for mf in files[:40]:
            if not mf.is_file():
                continue
            try:
                data = json.loads(mf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            chapters = data.get("chapters")
            if not isinstance(chapters, list) or not chapters:
                continue
            name = mf.parent.name.lower()
            score = 3 if (hint == "en" and "en-us" in name) or (hint == "zh" and "zh-cn" in name) else 1
            candidates.append((score, data))
    if not candidates:
        return {}
    best = max(candidates, key=lambda pair: pair[0])[1]
    titles = {
        str(c.get("id")): str(c.get("title") or "")
        for c in best["chapters"]
        if isinstance(c, dict) and c.get("id")
    }
    return {
        "titles": titles,
        "title": best.get("title") or "",
        "author": best.get("author") or "",
        "source": best.get("source") or {},
    }


def _normalize_reviewed_manifest(root: Path, manifest: dict, origin: Path | None) -> dict:
    """把 oz-reviewed-category-zip 的 files[] 归一化成标准 chapters[]。"""
    files = [f for f in (manifest.get("files") or []) if isinstance(f, dict)]
    if not files:
        raise PackageError("审核套包的 manifest.json 里没有 files[]")

    grouped: dict[str, dict] = {}
    order: list[str] = []
    for f in files:
        cid = str(f.get("chapter") or "").strip()
        rel = _safe_rel(f.get("archive_path") or "")
        if not cid:
            continue
        if cid not in grouped:
            grouped[cid] = {}
            order.append(cid)
        info: dict = {"path": rel}
        if f.get("sha256"):
            info["sha256"] = f["sha256"]
        if f.get("bytes"):
            info["bytes"] = f["bytes"]
        ext = Path(rel).suffix.lower()
        if ext in (".mp3", ".m4a", ".wav", ".aac"):
            grouped[cid]["audio"] = info
        elif ext == ".lrc":
            grouped[cid]["lrc"] = info
        elif ext == ".srt":
            grouped[cid]["srt"] = info
    if not order:
        raise PackageError("审核套包的 files[] 里没有可用章节")

    rels = [str(f.get("archive_path") or "") for f in files]
    meta = _sibling_metadata(root, origin, manifest)
    titles = meta.get("titles") or {}
    book = str(manifest.get("book") or root.name)
    category = str(manifest.get("category") or "")
    recorded = str((manifest.get("user_acceptance") or {}).get("recorded_at") or "")[:10]

    chapters: list[dict] = []
    for idx, cid in enumerate(order, 1):
        item = grouped[cid]
        if "audio" not in item:
            raise PackageError(f"审核套包 {cid} 缺少音频文件")
        lrc_rel = item.get("lrc", {}).get("path")
        scanned, seconds = _lrc_scan(root / lrc_rel) if lrc_rel else ("", 0.0)
        title = titles.get(cid) or scanned
        if title and not _CHAPTER_TITLE_RE.match(title):
            title = f"第{idx}章　{title}"  # LRC 里只有短标题（如「龙卷风」）时补章号
        raw: dict = {
            "id": cid,
            "title": title or f"第{idx}章",
            "duration_seconds": seconds,
            "audio": item["audio"],
            "images": [],
        }
        if "lrc" in item:
            raw["lrc"] = item["lrc"]
        if "srt" in item:
            raw["subtitles"] = {"srt": item["srt"]}
        chapters.append(raw)

    normalized = dict(manifest)  # 保留审计 / 来源信息
    normalized.update(
        {
            "title": f"{book} {category} · 审核版".strip(),
            "author": meta.get("author", ""),
            "language": _reviewed_language(manifest, rels),
            "version": f"reviewed-{recorded}" if recorded else "reviewed",
            "identifier": f"oz-reviewed/{book}-{category}" if category else f"oz-reviewed/{book}",
            "product_id": f"oz-reviewed/{book}-{category}" if category else f"oz-reviewed/{book}",
            "source": meta.get("source") or {},
            "reviewed_package": True,
            "chapters": chapters,
        }
    )
    return normalized


def parse_package(root: str | Path, *, verify_hash: bool = False) -> Package:
    """解析套包目录或审核包 ZIP，返回 Package。"""
    root = Path(root).expanduser()
    origin: Path | None = None
    if root.is_file() and root.suffix.lower() == ".zip":
        origin = root.resolve()
        root = _expand_zip(origin)
    root = root.resolve()
    if not root.is_dir():
        raise PackageError(f"套包目录不存在：{root}")

    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise PackageError(f"缺少 manifest.json：{manifest_path}")

    raw_text = manifest_path.read_text(encoding="utf-8")
    try:
        manifest = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise PackageError(f"manifest.json 解析失败：{exc}") from exc

    schema = manifest.get("schema", "")
    if schema and not any(schema.startswith(p) for p in SUPPORTED_SCHEMAS):
        raise PackageError(f"不支持的套包 schema：{schema}")
    if schema.startswith(REVIEWED_SCHEMA_PREFIX):
        manifest = _normalize_reviewed_manifest(root, manifest, origin)

    raw_chapters = manifest.get("chapters") or []
    if not raw_chapters:
        raise PackageError("manifest.json 中没有章节")

    chapters: list[Chapter] = []
    for idx, raw in enumerate(raw_chapters):
        chapters.append(_parse_chapter(root, raw, idx, verify_hash))

    return Package(
        root=root,
        manifest=manifest,
        chapters=chapters,
        manifest_sha256=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        origin=origin,
    )


def _rel_path(info: dict | None) -> str:
    """从一个资源描述块里取相对路径（兼容 str / dict 两种写法）。"""
    if not info:
        return ""
    if isinstance(info, str):
        return info
    return str(info.get("path") or "")


def _parse_chapter(root: Path, raw: dict, idx: int, verify_hash: bool) -> Chapter:
    cid = raw.get("id") or f"ch{idx + 1:02d}"
    title = raw.get("title") or cid

    audio_info = raw.get("audio") or {}
    audio_rel = _rel_path(audio_info)
    audio = (root / audio_rel) if audio_rel else root / cid / f"{cid}.mp3"

    ch = Chapter(
        id=cid,
        title=title,
        duration_seconds=float(raw.get("duration_seconds") or 0.0),
        audio=audio,
        audio_bytes=int(audio_info.get("bytes") or 0) if isinstance(audio_info, dict) else 0,
        audio_sha256=audio_info.get("sha256") if isinstance(audio_info, dict) else None,
        index=idx,
    )

    # 字幕：单语 SRT。不同版本的 manifest 字段名有过变化，
    # 这里把常见写法都认一遍，别因为字段名对不上就丢掉字幕。
    sub_info = raw.get("subtitles") or {}
    sub_rel = _rel_path(sub_info)
    if not sub_rel and isinstance(sub_info, dict):
        sub_rel = _rel_path(sub_info.get("srt")) or _rel_path(sub_info.get("primary"))
    if sub_rel:
        ch.srt = root / sub_rel

    # 双语 LRC
    lrc_rel = ""
    for key in ("bilingual_lrc", "lrc", "bilingual_subtitles"):
        lrc_rel = _rel_path(raw.get(key))
        if lrc_rel:
            break
    if not lrc_rel and isinstance(sub_info, dict):
        lrc_rel = _rel_path(sub_info.get("bilingual")) or _rel_path(sub_info.get("lrc"))
    if lrc_rel:
        ch.lrc = root / lrc_rel

    # 插图
    timeline = raw.get("picture_timeline") or []
    timeline_by_id = {t.get("image_id"): t for t in timeline if isinstance(t, dict)}
    for img in raw.get("images") or []:
        rel = img.get("path")
        if not rel:
            continue
        p = root / rel
        image_id = img.get("id") or p.stem
        tl = timeline_by_id.get(image_id) or {}
        ch.images.append(
            ImageAsset(
                path=p,
                image_id=image_id,
                start_ms=tl.get("start_ms"),
                sha256=img.get("sha256"),
            )
        )

    # 校验
    if not ch.audio.is_file():
        ch.errors.append(f"音频缺失：{ch.audio.name}")
    else:
        actual = ch.audio.stat().st_size
        if ch.audio_bytes and actual != ch.audio_bytes:
            ch.errors.append(f"音频大小不符：manifest {ch.audio_bytes} / 实际 {actual}")
        if verify_hash and ch.audio_sha256:
            if _sha256(ch.audio) != ch.audio_sha256:
                ch.errors.append("音频 sha256 校验失败")

    if ch.lrc and not ch.lrc.is_file():
        ch.errors.append(f"LRC 缺失：{ch.lrc.name}")
        ch.lrc = None
    if ch.srt and not ch.srt.is_file():
        ch.srt = None

    for img in ch.images:
        if not img.path.is_file():
            ch.errors.append(f"插图缺失：{img.path.name}")

    return ch


def iter_subtitle_tracks(chapter: Chapter) -> Iterator[SubtitleTrack]:
    """为一章产出可上传的字幕轨（优先双语拆分，其次回退 SRT）。"""
    if chapter.lrc:
        zh, en = parse_bilingual_lrc(chapter.lrc)
        if zh.cues:
            yield zh
        if en.cues and any(c.en for c in en.cues):
            yield en
        return

    if chapter.srt:
        for lang in ("zh-CN", "en-US"):
            track = SubtitleTrack(lang)
            for cue in _parse_srt(chapter.srt):
                setattr(cue, lang.split("-")[0], cue.zh or cue.en)
                track.cues.append(cue)
            if track.cues:
                yield track


def merge_bilingual_lrc(
    tracks: Iterable[SubtitleTrack], *, with_hours: bool = True
) -> str:
    """把中英字幕轨合并成平台要求的**单个** LRC 文件。

    平台上传页的提示原文：
        「请上传带有时间轴的LRC字幕文件（双语字幕请以 | 隔开，
          示例：[00:00:11.780] I've been looking for a reason | 我一直在寻找一个理由）」

    即：同一时间点只出一行，英文在前、中文在后，中间用 ` | ` 分隔。
    平台一次只接受一个字幕文件，所以不能提交 zh-CN / en-US 两个文件。

    with_hours=True 时输出 `[hh:mm:ss.mmm]`（与平台示例一致）。
    """
    merged: dict[int, dict[str, str]] = {}
    for track in tracks:
        for cue in track.cues:
            slot = merged.setdefault(cue.start_ms, {"zh": "", "en": ""})
            if cue.zh and not slot["zh"]:
                slot["zh"] = cue.zh.strip()
            if cue.en and not slot["en"]:
                slot["en"] = cue.en.strip()

    lines: list[str] = []
    for ms in sorted(merged):
        slot = merged[ms]
        en, zh = slot["en"], slot["zh"]
        if en and zh:
            text = f"{en} | {zh}"
        else:
            text = en or zh
        if not text:
            continue
        lines.append(f"[{_fmt_ts_hms(ms) if with_hours else _fmt_ts(ms)}]{text}")
    return "\n".join(lines) + "\n"


def _fmt_ts_hms(ms: int) -> str:
    """毫秒 -> LRC 时间戳 hh:mm:ss.mmm（与平台示例格式一致）。"""
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{milli:03d}"


def _parse_srt(path: Path) -> list[Cue]:
    """解析 SRT 为 Cue 列表。"""
    text = path.read_text(encoding="utf-8")
    cues: list[Cue] = []
    blocks = re.split(r"\n\s*\n", text.strip())
    ts_re = re.compile(
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})"
    )
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        m = ts_re.search(block)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600000 + g[1] * 60000 + g[2] * 1000 + g[3] * 10
        end = g[4] * 3600000 + g[5] * 60000 + g[6] * 1000 + g[7] * 10
        body = "\n".join(lines[1:])
        cues.append(Cue(start_ms=start, end_ms=end, zh=body))
    return cues


def summarize(pkg: Package) -> str:
    """人类可读的套包摘要。"""
    mb = pkg.total_bytes / 1024 / 1024
    mins = pkg.total_duration / 60
    version = f"　版本：{pkg.version}" if pkg.version else ""
    lines = [
        pkg.title,
        f"作者：{pkg.author}　语言：{pkg.language}{version}",
        f"章节：{len(pkg.chapters)}　总时长：{mins:.1f} 分钟　音频合计：{mb:.1f} MB",
        f"标识：{pkg.identifier or pkg.product_id}",
    ]
    if pkg.manifest.get("reviewed_package"):
        lines.append("格式：审核分类包（无插图，章节标题 / 时长 / AI 标记已自动补全）")
    if pkg.origin:
        lines.append(f"来源 ZIP：{pkg.origin.name}（已展开到 {pkg.root}）")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 套包发现
# --------------------------------------------------------------------------

# 材料库里套包目录的常见形状：<产品>-ximalaya-<语言>-rNNNN
_PKG_NAME_RE = re.compile(r"-r(\d{4})$")


def _release_rank(name: str) -> int:
    """从目录名里解析发布号（r0004 → 4），解析不出按 0。"""
    m = _PKG_NAME_RE.search(name)
    return int(m.group(1)) if m else 0


def find_packages(
    roots: Iterable[str | Path] | None = None, *, limit: int = 20, max_depth: int = 3
) -> list[Path]:
    """在若干个根目录里找出所有可解析的套包目录，按发布号从新到旧排。

    套包目录名带发布号（…-r0003 / …-r0004），素材换了版本之后目录也换名，
    工具不该再死记一个旧路径 —— 这里直接扫一遍最新的那个。
    搜索会向下最多 max_depth 层（套包通常在 <root>/<产品>/<套包> 里）。
    """
    if roots is None:
        roots = default_package_roots()
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        root = Path(root).expanduser()
        if not root.is_dir():
            continue
        # root 自身也算一层；审核包可能是目录，也可能是 .zip
        for cand in (root, *_walk_dirs(root, max_depth)):
            if "expanded_packages" in cand.parts:  # ZIP 解压缓存，不是源套包
                continue
            if cand not in seen and (cand / "manifest.json").is_file():
                seen.add(cand)
                found.append(cand)
            for z in sorted(cand.glob("*.zip")):
                if z in seen or not _zip_has_manifest(z):
                    continue
                seen.add(z)
                found.append(z)
    found.sort(
        key=lambda p: (_release_rank(p.name), 1 if "zh" in p.name.lower() else 0, p.name),
        reverse=True,
    )
    return found[:limit]


def _walk_dirs(root: Path, max_depth: int) -> Iterator[Path]:
    """自顶向下枚举目录（含 root 自身），深度受限。"""
    if max_depth <= 0:
        return
    try:
        children = sorted(root.iterdir())
    except OSError:
        return
    for child in children:
        if not child.is_dir() or child.name.startswith("."):
            continue
        yield child
        yield from _walk_dirs(child, max_depth - 1)


def default_package_roots() -> list[Path]:
    """默认的套包搜索根目录（制作端 materials 目录 + 旧的下载目录）。"""
    home = Path.home()
    return [
        home / "Documents" / "code" / "ebook_media_docs" / "materials" / "audio",
        home / "Documents" / "code" / "喜马拉雅上传工具" / "packages",
        home / "Downloads",
    ]


def latest_package(
    roots: Iterable[str | Path] | None = None,
) -> Path | None:
    """最新的一个套包目录（找不到返回 None）。"""
    pkgs = find_packages(roots)
    return pkgs[0] if pkgs else None


def find_reviewed_packages(roots: Iterable[str | Path] | None = None) -> list[Path]:
    """找出审核分类包（目录或 ZIP），供界面提示用户「有新套包可导入」。

    默认套包仍按发布号排序（r0004 优先），不会自作主张换成审核包 ——
    换套包可能影响断点续传和专辑归属，必须让用户明确选择。
    """
    out: list[Path] = []
    for path in find_packages(roots):
        schema = ""
        try:
            if path.suffix.lower() == ".zip":
                with zipfile.ZipFile(path) as zf:
                    name = next(
                        (
                            n
                            for n in zf.namelist()
                            if Path(n).name == "manifest.json" and n.count("/") <= 1
                        ),
                        None,
                    )
                    if name:
                        schema = json.loads(zf.read(name).decode("utf-8")).get("schema", "")
            else:
                schema = json.loads(
                    (path / "manifest.json").read_text(encoding="utf-8")
                ).get("schema", "")
        except (OSError, ValueError, zipfile.BadZipFile):
            continue
        if str(schema).startswith(REVIEWED_SCHEMA_PREFIX):
            out.append(path)
    return out

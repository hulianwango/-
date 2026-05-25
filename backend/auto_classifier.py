from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from . import paper_files
from .database import db_session, utcnow
from .indexer import hash_file


AUTO_FALLBACK_CATEGORY = "待分类"
AUTO_CLASSIFY_MIN_SCORE = 2
AUTO_MOVE_MIN_SCORE = 4
STRICT_AUTO_MOVE_MIN_SCORE = 8
STRICT_MIN_DIMENSION_SCORE = 2
STRICT_AMBIGUITY_MARGIN = 2
STRICT_HIERARCHY_ORDER = "mechanism/material_structure/application"
STRICT_REVIEW_POLICY = "review"
STRICT_DUPLICATE_POLICY = "classify"
STRICT_TARGET_PREFIX = ""
BEST_GUESS_REVIEW_POLICY = "best_guess"
DUPLICATE_ZONE_POLICY = "duplicate_zone"
DUPLICATE_ZONE_CATEGORY = "重复PDF"
ORIGINAL_RENAME_POLICY = "original"
CONTENT_SUMMARY_RENAME_POLICY = "content_summary"
CHINESE_BRIEF_WORK_RENAME_POLICY = "chinese_brief_work"
MAX_CONTENT_FILENAME_STEM_LENGTH = 120
UNKNOWN_MECHANISM_LABEL = "机制待确认"
UNKNOWN_MATERIAL_LABEL = "材料结构待确认"

AUTO_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("卟啉性能研究的文献", ("porphyrin", "porphyrinic", "卟啉")),
    ("菌叶绿素相关", ("bacteriochlorin", "chlorophyll", "叶绿素")),
    ("电致发光", ("electrolumines", "light-emitting diode", "white-light-emitting", "led")),
    ("plasmon研究", ("plasmon", "plasmonic", "gold nanoparticle", "aunp", " gold ", " au ")),
    (
        "COF",
        (
            "covalent organic framework",
            "covalent-organic framework",
            "covalent organic frameworks",
            "cof",
            "cofs",
            "vinylene-linked",
        ),
    ),
    (
        "MOF",
        (
            "metal-organic framework",
            "metal-organic framework",
            "metal organic framework",
            "mof",
            "mofs",
        ),
    ),
    (
        "上转换纳米颗粒",
        (
            "upconversion",
            "upconverting",
            "ucnp",
            "ucnps",
            "nayf4",
            "nabif4",
            "yb3",
            "er3",
            "tm3",
            "nd3",
            "lanthanide",
        ),
    ),
    (
        "光动力/肿瘤治疗",
        (
            "photodynamic",
            "cancer therapy",
            "tumor",
            "theranostic",
            "doxorubicin",
            "singlet oxygen",
            "microrobot",
        ),
    ),
    ("温度传感", ("thermometry", "temperature sensing", "thermal quenching", "temperature regions")),
)


@dataclass(frozen=True)
class StrictRule:
    label: str
    aliases: tuple[str, ...]


STRICT_MECHANISM_RULES: tuple[StrictRule, ...] = (
    StrictRule(
        "上转换发光机制",
        (
            "upconversion",
            "upconverting",
            "anti-stokes",
            "lanthanide-doped",
            "rare-earth",
            "rare earth",
            "nayf4",
            "nagdf4",
            "nabif4",
            "yb3",
            "er3",
            "tm3",
            "nd3",
        ),
    ),
    StrictRule(
        "等离激元增强机制",
        (
            "plasmon",
            "plasmonic",
            "localized surface plasmon resonance",
            "lspr",
            "metal-enhanced",
            "nanoantenna",
            "hot electron",
            "gold nanoparticle",
            "aunp",
            "silver nanoparticle",
            "agnp",
            "nanorod",
            "nanostar",
        ),
    ),
    StrictRule(
        "框架限域与能量转移",
        (
            "metal-organic framework",
            "metal organic framework",
            "covalent organic framework",
            "covalent-organic framework",
            "mof",
            "mofs",
            "cof",
            "cofs",
            "energy transfer",
            "fret",
            "antenna effect",
            "sensitized emission",
            "host-guest",
            "coordination framework",
        ),
    ),
    StrictRule(
        "光敏产生活性氧",
        (
            "photodynamic",
            "photosensitizer",
            "singlet oxygen",
            "reactive oxygen species",
            "ros",
            "1o2",
            "type i",
            "type ii",
            "tumor ablation",
        ),
    ),
    StrictRule(
        "分子光物理与π扩展",
        (
            "porphyrin",
            "porphyrinic",
            "phthalocyanine",
            "chlorin",
            "bacteriochlorin",
            "pi-extended",
            "π-extended",
            "photophysical",
            "fluorescence",
            "absorption",
        ),
    ),
    StrictRule(
        "电致发光机制",
        (
            "electroluminescence",
            "electroluminescent",
            "light-emitting diode",
            "white-light-emitting",
            "oled",
            "led",
        ),
    ),
    StrictRule(
        "温度响应发光",
        (
            "thermometry",
            "temperature sensing",
            "thermal sensing",
            "thermal quenching",
            "luminescence thermometer",
            "temperature-dependent",
        ),
    ),
)

STRICT_MATERIAL_RULES: tuple[StrictRule, ...] = (
    StrictRule(
        "核壳/多层核壳结构",
        (
            "core-shell",
            "core/shell",
            "core shell",
            "multilayer",
            "multi-layer",
            "active shell",
            "inert shell",
            "shell thickness",
            "shell growth",
        ),
    ),
    StrictRule(
        "稀土掺杂纳米颗粒",
        (
            "upconversion nanoparticle",
            "upconverting nanoparticle",
            "ucnp",
            "ucnps",
            "lanthanide-doped nanoparticle",
            "rare-earth doped",
            "nayf4",
            "nagdf4",
            "nabif4",
            "yb3",
            "er3",
            "tm3",
            "nd3",
        ),
    ),
    StrictRule(
        "贵金属纳米结构",
        (
            "gold nanoparticle",
            "gold nanorod",
            "gold nanostar",
            "gold nanoshell",
            "aunp",
            "silver nanoparticle",
            "agnp",
            "plasmonic nanoparticle",
            "noble metal",
            "nanoantenna",
            "nanorod",
            "nanostar",
        ),
    ),
    StrictRule(
        "MOF",
        (
            "metal-organic framework",
            "metal organic framework",
            "porphyrinic mof",
            "mof",
            "mofs",
            "coordination framework",
        ),
    ),
    StrictRule(
        "COF",
        (
            "covalent organic framework",
            "covalent-organic framework",
            "vinylene-linked",
            "cof",
            "cofs",
        ),
    ),
    StrictRule(
        "菌叶绿素",
        (
            "bacteriochlorophyll",
            "bacteriochlorin",
            "chlorophyll",
            "叶绿素",
        ),
    ),
    StrictRule(
        "卟啉/酞菁/氯啉",
        (
            "porphyrin",
            "porphyrinic",
            "phthalocyanine",
            "chlorin",
            "porphine",
            "卟啉",
        ),
    ),
    StrictRule(
        "有机发光材料",
        (
            "organic light-emitting",
            "light-emitting material",
            "tadf",
            "phosphorescent",
            "fluorescent molecule",
            "iridium complex",
            "polymer light-emitting",
        ),
    ),
)

STRICT_APPLICATION_RULES: tuple[StrictRule, ...] = (
    StrictRule(
        "光动力肿瘤治疗",
        (
            "photodynamic therapy",
            "photodynamic",
            "pdt",
            "tumor",
            "cancer therapy",
            "anticancer",
            "theranostic",
            "in vivo therapy",
        ),
    ),
    StrictRule(
        "温度传感",
        (
            "temperature sensing",
            "thermometry",
            "thermal sensing",
            "luminescence thermometer",
        ),
    ),
    StrictRule(
        "生物成像",
        (
            "bioimaging",
            "biological imaging",
            "cellular imaging",
            "in vivo imaging",
            "fluorescence imaging",
            "imaging-guided",
        ),
    ),
    StrictRule(
        "电致发光器件",
        (
            "light-emitting diode",
            "white-light-emitting",
            "oled",
            "led device",
            "electroluminescent device",
        ),
    ),
    StrictRule(
        "能量转移基础研究",
        (
            "energy transfer",
            "fret",
            "antenna effect",
            "sensitized emission",
            "luminescence resonance energy transfer",
        ),
    ),
    StrictRule(
        "材料性能研究",
        (
            "optical properties",
            "luminescence properties",
            "photophysical properties",
            "synthesis",
            "characterization",
            "performance",
        ),
    ),
)

STRICT_PATH_SEGMENTS = {
    "核壳/多层核壳结构": "核壳-多层核壳结构",
    "卟啉/酞菁/氯啉": "卟啉-酞菁-氯啉",
}


def _alias_hits(text: str, alias: str) -> int:
    haystack = f" {text.casefold()} "
    needle = alias.casefold()
    if not needle.strip():
        return 0
    if re.fullmatch(r"[a-z0-9]{2,6}", needle):
        return len(re.findall(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack))
    return haystack.count(needle)


def _paper_text_sample(connection, paper_id: str, page_limit: int = 3) -> str:  # type: ignore[no-untyped-def]
    rows = connection.execute(
        """
        SELECT text
        FROM paper_pages
        WHERE paper_id = ?
        ORDER BY page_number
        LIMIT ?
        """,
        (paper_id, page_limit),
    ).fetchall()
    return "\n".join(row["text"] or "" for row in rows)


def _score_rules(
    title_text: str,
    body_text: str,
    rules: tuple[StrictRule, ...],
) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    for rule in rules:
        score = 0
        reasons: list[str] = []
        for alias in rule.aliases:
            title_hits = _alias_hits(title_text, alias)
            body_hits = _alias_hits(body_text, alias)
            if not title_hits and not body_hits:
                continue
            score += title_hits * 6 + min(body_hits, 5)
            reasons.append(alias)
        if score:
            scored.append({"label": rule.label, "score": score, "reasons": reasons[:5]})
    return sorted(scored, key=lambda item: (-int(item["score"]), str(item["label"])))


def _best_score(scored: list[dict[str, Any]], fallback_label: str = "") -> dict[str, Any]:
    if scored:
        return scored[0]
    return {"label": fallback_label, "score": 0, "reasons": []}


def _is_ambiguous(scored: list[dict[str, Any]]) -> bool:
    if len(scored) < 2:
        return False
    best = int(scored[0]["score"])
    second = int(scored[1]["score"])
    return second > 0 and best - second <= STRICT_AMBIGUITY_MARGIN


def _path_segment(label: str) -> str:
    return STRICT_PATH_SEGMENTS.get(label, label)


def _strict_category_path(mechanism: str, material_structure: str, application: str) -> str:
    return paper_files.normalize_category_path(
        "/".join(_path_segment(part) for part in (mechanism, material_structure, application))
    )


def _strict_best_guess_category_path(
    mechanism: str,
    material_structure: str,
    application: str,
) -> str:
    return _strict_category_path(
        mechanism or UNKNOWN_MECHANISM_LABEL,
        material_structure or UNKNOWN_MATERIAL_LABEL,
        application or "基础性能研究",
    )


def _prefixed_category_path(target_prefix: str, category_path: str) -> str:
    prefix = paper_files.normalize_category_path(target_prefix)
    category_path = paper_files.normalize_category_path(category_path)
    if not prefix:
        return category_path
    if not category_path:
        return prefix
    return paper_files.normalize_category_path(f"{prefix}/{category_path}")


def _relative_path(path: Path) -> str:
    root = paper_files.papers_root()
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _source_item(path: Path) -> dict[str, str]:
    return {
        "path": _relative_path(path),
        "name": path.name,
        "category_path": paper_files.category_path_for_file(path),
    }


def _safe_filename_stem(value: Any, *, fallback: str = "paper", max_length: int = 80) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "-", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"-{2,}", "-", text)
    text = text.strip(" .-_")
    if not text:
        text = fallback
    if len(text) > max_length:
        text = text[:max_length].rstrip(" .-_")
    return text or fallback


def _safe_target_filename(filename: str, *, source_path: Path, file_hash: str) -> str:
    suffix = source_path.suffix if source_path.suffix.lower() == ".pdf" else ".pdf"
    raw = str(filename or source_path.name).replace("\\", "/").split("/")[-1]
    stem = Path(raw).stem if raw else ""
    stem = _safe_filename_stem(
        stem,
        fallback=_safe_filename_stem(source_path.stem, fallback=file_hash[:12]),
        max_length=MAX_CONTENT_FILENAME_STEM_LENGTH,
    )
    return f"{stem}{suffix}"


def _content_summary_filename(
    *,
    source_path: Path,
    file_hash: str,
    row: Any,
    decision: dict[str, Any],
) -> str:
    year = str(row["year"]).strip() if "year" in row.keys() and row["year"] else ""
    labels = [
        decision.get("mechanism") or UNKNOWN_MECHANISM_LABEL,
        decision.get("material_structure") or UNKNOWN_MATERIAL_LABEL,
        decision.get("application") or "基础性能研究",
    ]
    title = row["title"] if "title" in row.keys() and row["title"] else source_path.stem
    segments = [
        _safe_filename_stem(year, fallback="", max_length=8) if year else "",
        *(_safe_filename_stem(label, max_length=24) for label in labels),
        _safe_filename_stem(title, fallback=file_hash[:12], max_length=72),
    ]
    stem = " - ".join(segment for segment in segments if segment)
    stem = _safe_filename_stem(stem, fallback=file_hash[:12], max_length=MAX_CONTENT_FILENAME_STEM_LENGTH)
    return f"{stem}.pdf"


def _first_matching_phrase(text: str, rules: tuple[tuple[tuple[str, ...], str], ...], fallback: str) -> str:
    lowered = text.lower()
    for aliases, phrase in rules:
        if any(alias in lowered for alias in aliases):
            return phrase
    return fallback


def _label_text(decision: dict[str, Any], key: str, fallback: str) -> str:
    value = str(decision.get(key) or "").strip()
    return value or fallback


def _brief_work_phrase(row: Any, decision: dict[str, Any], sample_text: str = "") -> str:
    title = str(row["title"] or "") if "title" in row.keys() else ""
    journal = str(row["journal"] or "") if "journal" in row.keys() else ""
    doi = str(row["doi"] or "") if "doi" in row.keys() else ""
    haystack = " ".join(
        [
            title,
            journal,
            doi,
            sample_text[:5000],
            _label_text(decision, "mechanism", ""),
            _label_text(decision, "material_structure", ""),
            _label_text(decision, "application", ""),
        ]
    )

    material = _first_matching_phrase(
        haystack,
        (
            (("core-shell", "core shell", "shell", "multilayer", "核壳"), "核壳上转换纳米颗粒"),
            (("upconversion", "upconverting", "ucnp", "nayf4", "lanthanide", "稀土"), "稀土掺杂上转换纳米颗粒"),
            (("gold nanorod", "gold nanoparticle", "aunp", "plasmon", "贵金属"), "贵金属等离激元纳米结构"),
            (("metal-organic framework", "mof", "金属有机框架"), "MOF框架发光材料"),
            (("covalent organic framework", "cof", "共价有机框架"), "COF框架光功能材料"),
            (("porphyrin", "phthalocyanine", "chlorin", "卟啉", "酞菁", "氯啉"), "卟啉/酞菁光敏分子"),
            (("bacteriochlorin", "chlorophyll", "菌叶绿素"), "菌叶绿素相关光敏材料"),
            (("oled", "electrolumines", "light-emitting diode", "电致发光"), "有机电致发光材料"),
        ),
        _label_text(decision, "material_structure", "光功能材料"),
    )

    mechanism = _first_matching_phrase(
        haystack,
        (
            (("photodynamic", "reactive oxygen", "singlet oxygen", "ros", "光动力"), "产生活性氧"),
            (("plasmon", "lspr", "surface plasmon", "等离激元"), "等离激元增强发光"),
            (("energy transfer", "fret", "能量转移"), "调控能量转移"),
            (("upconversion", "upconverting", "上转换"), "上转换发光"),
            (("temperature", "thermometry", "thermal", "温度"), "温度响应发光"),
            (("electrolumines", "oled", "电致发光"), "电致发光"),
        ),
        _label_text(decision, "mechanism", "光物理性能"),
    )

    application = _first_matching_phrase(
        haystack,
        (
            (("photodynamic", "tumor", "cancer", "therapy", "光动力", "肿瘤"), "用于光动力肿瘤治疗"),
            (("temperature", "thermometry", "sensor", "sensing", "温度", "传感"), "用于温度传感"),
            (("imaging", "bioimaging", "生物成像"), "用于生物成像"),
            (("oled", "device", "electrolumines", "器件"), "用于电致发光器件"),
            (("energy transfer", "fret", "mechanism", "能量转移"), "解析能量转移机制"),
        ),
        _label_text(decision, "application", "开展基础性能研究"),
    )

    lowered_title = title.lower()
    if any(term in lowered_title for term in ("review", "progress", "perspective", "综述")):
        phrase = f"综述{material}的{mechanism}与{application}"
    elif any(term in lowered_title for term in ("enhanc", "improv", "boost", "增强", "提升")):
        phrase = f"增强{material}的{mechanism}性能"
    elif any(term in lowered_title for term in ("synth", "fabricat", "construct", "prepare", "构建", "制备")):
        phrase = f"构建{material}{application}"
    else:
        phrase = f"研究{material}的{mechanism}及{application}"
    return _safe_filename_stem(phrase, fallback="文献工作内容待识别", max_length=56)


def _short_title_or_hash(row: Any, source_path: Path, file_hash: str) -> str:
    title = str(row["title"] or "") if "title" in row.keys() and row["title"] else source_path.stem
    title = re.sub(r"\b(pdf|supporting information)\b", " ", title, flags=re.IGNORECASE)
    return _safe_filename_stem(title, fallback=file_hash[:12], max_length=42)


def _chinese_brief_work_filename(
    *,
    source_path: Path,
    file_hash: str,
    row: Any,
    decision: dict[str, Any],
) -> str:
    year = str(row["year"]).strip() if "year" in row.keys() and row["year"] else ""
    work = decision.get("chinese_brief_work") or _brief_work_phrase(row, decision)
    title = _short_title_or_hash(row, source_path, file_hash)
    segments = [
        _safe_filename_stem(year, fallback="", max_length=8) if year else "",
        _safe_filename_stem(work, fallback="文献工作内容待识别", max_length=56),
        title,
    ]
    stem = " - ".join(segment for segment in segments if segment)
    stem = _safe_filename_stem(stem, fallback=file_hash[:12], max_length=MAX_CONTENT_FILENAME_STEM_LENGTH)
    return f"{stem}.pdf"


def _preferred_target_filename(
    *,
    source_path: Path,
    file_hash: str,
    db_row: Any,
    decision: dict[str, Any],
    rename_policy: str,
) -> str:
    if rename_policy == CHINESE_BRIEF_WORK_RENAME_POLICY:
        return _chinese_brief_work_filename(
            source_path=source_path,
            file_hash=file_hash,
            row=db_row,
            decision=decision,
        )
    if rename_policy == CONTENT_SUMMARY_RENAME_POLICY:
        return _content_summary_filename(
            source_path=source_path,
            file_hash=file_hash,
            row=db_row,
            decision=decision,
        )
    return source_path.name


def _reserve_unique_target_path(
    source_path: Path,
    target_dir: Path,
    file_hash: str,
    reserved_targets: set[str],
    preferred_name: str | None = None,
) -> Path:
    source_path = source_path.resolve()
    target_dir = paper_files._require_inside_papers_root(  # type: ignore[attr-defined]
        target_dir.resolve(),
        "target folder must stay inside the papers folder.",
    )
    target_filename = _safe_target_filename(
        preferred_name or source_path.name,
        source_path=source_path,
        file_hash=file_hash,
    )
    preferred = paper_files._require_inside_papers_root(  # type: ignore[attr-defined]
        (target_dir / target_filename).resolve(),
        "target file path must stay inside the papers folder.",
    )

    def available(candidate: Path) -> bool:
        key = str(candidate.resolve()).casefold()
        if candidate.resolve() == source_path:
            return True
        return key not in reserved_targets and not candidate.exists()

    if available(preferred):
        reserved_targets.add(str(preferred.resolve()).casefold())
        return preferred

    stem = preferred.stem.rstrip(" .") or file_hash[:12]
    suffix = preferred.suffix or ".pdf"
    for index in range(1, 1000):
        candidate = paper_files._require_inside_papers_root(  # type: ignore[attr-defined]
            (target_dir / f"{stem}.strict-{file_hash[:8]}-{index}{suffix}").resolve(),
            "target file path must stay inside the papers folder.",
        )
        if available(candidate):
            reserved_targets.add(str(candidate.resolve()).casefold())
            return candidate
    raise HTTPException(status_code=409, detail="could not reserve a unique target path.")


def _move_database_paper(
    *,
    paper_id: str,
    source_path: Path,
    target_path: Path,
    dry_run: bool,
) -> str:
    source_path = source_path.resolve()
    target_path = target_path.resolve()
    if source_path == target_path:
        return "unchanged"
    if dry_run:
        return "move"
    if target_path.exists():
        raise HTTPException(status_code=409, detail="target_file_exists")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.rename(target_path)
    try:
        with db_session() as connection:
            connection.execute(
                """
                UPDATE papers
                SET file_path = ?, updated_at = ?
                WHERE paper_id = ?
                """,
                (str(target_path), utcnow(), paper_id),
            )
    except Exception:
        try:
            if target_path.exists() and not source_path.exists():
                target_path.rename(source_path)
        except Exception:
            pass
        raise
    return "moved"


def _move_pdf_file(source_path: Path, target_path: Path, dry_run: bool) -> str:
    source_path = source_path.resolve()
    target_path = target_path.resolve()
    if source_path == target_path:
        return "unchanged"
    if dry_run:
        return "move"
    if target_path.exists():
        raise HTTPException(status_code=409, detail="target_file_exists")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.rename(target_path)
    return "moved"


def classify_paper_record(row, sample_text: str = "") -> dict[str, Any]:  # type: ignore[no-untyped-def]
    title_text = " ".join(
        str(row[key] or "")
        for key in ("title", "authors", "journal", "doi")
        if key in row.keys()
    )
    body_text = sample_text or ""
    best_category = AUTO_FALLBACK_CATEGORY
    best_score = 0
    best_reasons: list[str] = []

    for category_path, aliases in AUTO_CATEGORY_RULES:
        score = 0
        reasons: list[str] = []
        for alias in aliases:
            title_hits = _alias_hits(title_text, alias)
            body_hits = _alias_hits(body_text, alias)
            if title_hits or body_hits:
                score += title_hits * 4 + min(body_hits, 3)
                reasons.append(alias)
        if score > best_score:
            best_category = category_path
            best_score = score
            best_reasons = reasons

    if best_score < AUTO_CLASSIFY_MIN_SCORE:
        best_category = AUTO_FALLBACK_CATEGORY
        best_reasons = []

    return {
        "category_path": paper_files.normalize_category_path(best_category),
        "score": best_score,
        "reason": ", ".join(best_reasons[:4]) if best_reasons else "low-confidence fallback",
    }


def classify_paper_record_strict(row, sample_text: str = "") -> dict[str, Any]:  # type: ignore[no-untyped-def]
    title_text = " ".join(
        str(row[key] or "")
        for key in ("title", "authors", "journal", "doi")
        if key in row.keys()
    )
    body_text = sample_text or ""

    mechanism_scores = _score_rules(title_text, body_text, STRICT_MECHANISM_RULES)
    material_scores = _score_rules(title_text, body_text, STRICT_MATERIAL_RULES)
    application_scores = _score_rules(title_text, body_text, STRICT_APPLICATION_RULES)

    mechanism = _best_score(mechanism_scores)
    material = _best_score(material_scores)
    application = _best_score(application_scores, "基础性能研究")
    application_label = application["label"] or "基础性能研究"
    application_score = int(application["score"])
    total_score = int(mechanism["score"]) + int(material["score"]) + application_score

    needs_review_reasons: list[str] = []
    if int(mechanism["score"]) < STRICT_MIN_DIMENSION_SCORE:
        needs_review_reasons.append("missing_or_weak_mechanism")
    if int(material["score"]) < STRICT_MIN_DIMENSION_SCORE:
        needs_review_reasons.append("missing_or_weak_material_structure")
    if _is_ambiguous(mechanism_scores):
        needs_review_reasons.append("multi_topic_mechanism")
    if _is_ambiguous(material_scores):
        needs_review_reasons.append("multi_topic_material_structure")

    target_category = (
        _strict_category_path(mechanism["label"], material["label"], application_label)
        if not needs_review_reasons
        else AUTO_FALLBACK_CATEGORY
    )
    best_guess_category = _strict_best_guess_category_path(
        mechanism["label"],
        material["label"],
        application_label,
    )
    reason_parts = [
        f"机制:{mechanism['label'] or '未识别'}({mechanism['score']})",
        f"材料结构:{material['label'] or '未识别'}({material['score']})",
        f"应用:{application_label}({application_score})",
    ]

    return {
        "category_path": target_category,
        "best_guess_category_path": best_guess_category,
        "score": total_score,
        "reason": "; ".join(reason_parts),
        "mechanism": mechanism["label"],
        "mechanism_score": int(mechanism["score"]),
        "mechanism_reasons": mechanism["reasons"],
        "material_structure": material["label"],
        "material_structure_score": int(material["score"]),
        "material_structure_reasons": material["reasons"],
        "application": application_label,
        "application_score": application_score,
        "application_reasons": application["reasons"],
        "needs_review_reasons": needs_review_reasons,
        "mechanism_candidates": mechanism_scores[:3],
        "material_structure_candidates": material_scores[:3],
        "application_candidates": application_scores[:3],
    }


def _db_paper_rows(paper_ids: list[str] | None = None) -> tuple[list[Any], dict[str, str]]:
    params: tuple[Any, ...] = ()
    where = ""
    if paper_ids:
        unique_ids = list(dict.fromkeys(paper_ids))
        placeholders = ",".join("?" for _ in unique_ids)
        where = f"WHERE paper_id IN ({placeholders})"
        params = tuple(unique_ids)

    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT
                paper_id, title, authors, year, journal, doi, file_path, file_hash,
                page_count, created_at, updated_at
            FROM papers
            {where}
            ORDER BY updated_at DESC
            """,
            params,
        ).fetchall()
        sample_texts = {
            row["paper_id"]: _paper_text_sample(connection, row["paper_id"], page_limit=5)
            for row in rows
        }
    return rows, sample_texts


def _pdf_files() -> list[Path]:
    root = paper_files.papers_root()
    return sorted(
        (path.resolve() for path in root.rglob("*") if path.is_file() and path.suffix.lower() == ".pdf"),
        key=lambda path: str(path).casefold(),
    )


def classify_library_strict(
    *,
    dry_run: bool = False,
    limit: int | None = None,
    paper_ids: list[str] | None = None,
    source_paths: list[str | Path] | None = None,
    include_paper_paths: bool = True,
    include_classified: bool = True,
    include_duplicates: bool = True,
    min_auto_score: int = STRICT_AUTO_MOVE_MIN_SCORE,
    hierarchy_order: str = STRICT_HIERARCHY_ORDER,
    target_prefix: str = STRICT_TARGET_PREFIX,
    review_policy: str = STRICT_REVIEW_POLICY,
    duplicate_policy: str = STRICT_DUPLICATE_POLICY,
    rename_policy: str = ORIGINAL_RENAME_POLICY,
) -> dict[str, Any]:
    if hierarchy_order != STRICT_HIERARCHY_ORDER:
        raise HTTPException(
            status_code=400,
            detail=f"hierarchy_order must be {STRICT_HIERARCHY_ORDER}.",
        )
    if review_policy not in {STRICT_REVIEW_POLICY, BEST_GUESS_REVIEW_POLICY}:
        raise HTTPException(status_code=400, detail="review_policy must be review or best_guess.")
    if duplicate_policy not in {STRICT_DUPLICATE_POLICY, DUPLICATE_ZONE_POLICY}:
        raise HTTPException(status_code=400, detail="duplicate_policy must be classify or duplicate_zone.")
    if rename_policy not in {
        ORIGINAL_RENAME_POLICY,
        CONTENT_SUMMARY_RENAME_POLICY,
        CHINESE_BRIEF_WORK_RENAME_POLICY,
    }:
        raise HTTPException(
            status_code=400,
            detail="rename_policy must be original, content_summary, or chinese_brief_work.",
        )
    target_prefix = paper_files.normalize_category_path(target_prefix)
    if target_prefix:
        paper_files.category_dir(target_prefix)
    min_auto_score = max(STRICT_AUTO_MOVE_MIN_SCORE, int(min_auto_score or STRICT_AUTO_MOVE_MIN_SCORE))
    rows, sample_texts = _db_paper_rows(paper_ids)
    root = paper_files.papers_root()
    row_by_path = {str(Path(row["file_path"]).resolve()).casefold(): row for row in rows}
    row_by_hash = {row["file_hash"]: row for row in rows}
    decisions: dict[str, dict[str, Any]] = {}
    for row in rows:
        sample_text = sample_texts.get(row["paper_id"], "")
        decision = classify_paper_record_strict(row, sample_text)
        decision["chinese_brief_work"] = _brief_work_phrase(row, decision, sample_text)
        decisions[row["paper_id"]] = decision

    extra_source_paths = [
        Path(path).resolve()
        for path in (source_paths or [])
        if str(path or "").strip()
    ]
    if include_duplicates and paper_ids is None and not extra_source_paths:
        pdfs = _pdf_files()
    else:
        pdfs = [Path(row["file_path"]).resolve() for row in rows] if include_paper_paths else []
        pdfs.extend(extra_source_paths)

    results: list[dict[str, Any]] = []
    moved = 0
    unchanged = 0
    skipped_review = 0
    conflicts = 0
    failed = 0
    planned = 0
    duplicate_count = 0
    unknown_count = 0
    renamed = 0
    reserved_targets: set[str] = set()

    for source_path in pdfs:
        if limit is not None and len(results) >= max(1, int(limit)):
            break
        source_path = source_path.resolve()
        try:
            file_hash = hash_file(source_path)
        except Exception as exc:
            failed += 1
            results.append(
                {
                    "type": "unknown_pdf",
                    "status": "failed",
                    "source": _source_item(source_path),
                    "error": type(exc).__name__,
                }
            )
            continue

        db_row = row_by_path.get(str(source_path).casefold())
        duplicate = False
        if db_row is None:
            db_row = row_by_hash.get(file_hash)
            duplicate = db_row is not None

        source = _source_item(source_path)
        if db_row is None:
            unknown_count += 1
            skipped_review += 1
            results.append(
                {
                    "type": "unknown_pdf",
                    "status": "needs_review",
                    "source": source,
                    "file_hash": file_hash,
                    "reason": "not indexed and no matching database hash",
                    "needs_review_reasons": ["not_indexed"],
                }
            )
            continue

        decision = decisions[db_row["paper_id"]]
        target_category = decision["category_path"]
        base_item = {
            "type": "duplicate_file" if duplicate else "database_paper",
            "duplicate": duplicate,
            "paper_id": db_row["paper_id"],
            "matches_paper_id": db_row["paper_id"] if duplicate else None,
            "title": db_row["title"],
            "source": source,
            "source_path": source["path"],
            "source_category_path": source["category_path"],
            "target_category_path": target_category,
            "score": decision["score"],
            "reason": decision["reason"],
            "file_hash": file_hash,
            "strict": {
                "mechanism": decision["mechanism"],
                "mechanism_score": decision["mechanism_score"],
                "mechanism_reasons": decision["mechanism_reasons"],
                "material_structure": decision["material_structure"],
                "material_structure_score": decision["material_structure_score"],
                "material_structure_reasons": decision["material_structure_reasons"],
                "application": decision["application"],
                "application_score": decision["application_score"],
                "application_reasons": decision["application_reasons"],
                "chinese_brief_work": decision.get("chinese_brief_work", ""),
                "needs_review_reasons": decision["needs_review_reasons"],
            },
        }
        if duplicate:
            duplicate_count += 1
        elif source["category_path"] and not include_classified:
            unchanged += 1
            results.append({**base_item, "status": "unchanged"})
            continue

        needs_review = list(decision["needs_review_reasons"])
        if int(decision["score"]) < min_auto_score:
            needs_review.append("low_total_score")
        if target_category in {"", AUTO_FALLBACK_CATEGORY}:
            needs_review.append("fallback_category")
        best_guess = False
        if needs_review and review_policy == BEST_GUESS_REVIEW_POLICY:
            target_category = decision["best_guess_category_path"]
            best_guess = True
        if needs_review and review_policy != BEST_GUESS_REVIEW_POLICY:
            skipped_review += 1
            review_status = (
                "multi_topic_review"
                if any(reason.startswith("multi_topic") for reason in needs_review)
                else "needs_review"
            )
            results.append(
                {
                    **base_item,
                    "status": review_status,
                    "needs_review_reasons": list(dict.fromkeys(needs_review)),
                }
            )
            continue

        try:
            if duplicate and duplicate_policy == DUPLICATE_ZONE_POLICY:
                final_target_category = _prefixed_category_path(target_prefix, DUPLICATE_ZONE_CATEGORY)
            else:
                final_target_category = _prefixed_category_path(target_prefix, target_category)
            target_dir = paper_files.category_dir(final_target_category)
            target_filename = _preferred_target_filename(
                source_path=source_path,
                file_hash=file_hash,
                db_row=db_row,
                decision=decision,
                rename_policy=rename_policy,
            )
            if (
                duplicate
                and duplicate_policy == DUPLICATE_ZONE_POLICY
                and rename_policy == CHINESE_BRIEF_WORK_RENAME_POLICY
            ):
                target_filename = f"重复 - {target_filename}"
            target_path = _reserve_unique_target_path(
                source_path,
                target_dir,
                file_hash,
                reserved_targets,
                preferred_name=target_filename,
            )
            item = {
                **base_item,
                "best_guess": best_guess,
                "needs_review_reasons": list(dict.fromkeys(needs_review)),
                "review_policy": review_policy,
                "duplicate_policy": duplicate_policy,
                "rename_policy": rename_policy,
                "target_category_path": final_target_category,
                "target": {
                    "path": target_path.relative_to(root).as_posix(),
                    "name": target_path.name,
                    "category_path": paper_files.category_path_for_file(target_path),
                },
                "target_path": target_path.relative_to(root).as_posix(),
                "target_filename": target_path.name,
                "renamed": source_path.name != target_path.name,
                "min_auto_score": int(min_auto_score),
            }
            if duplicate:
                status = _move_pdf_file(source_path, target_path, dry_run)
            else:
                status = _move_database_paper(
                    paper_id=db_row["paper_id"],
                    source_path=source_path,
                    target_path=target_path,
                    dry_run=dry_run,
                )
            item["status"] = status
            if item["renamed"] and status in {"move", "moved"}:
                renamed += 1
            if status == "move":
                planned += 1
            elif status == "moved":
                moved += 1
            else:
                unchanged += 1
            results.append(item)
        except HTTPException as exc:
            conflicts += 1 if exc.status_code == 409 else 0
            failed += 0 if exc.status_code == 409 else 1
            results.append({**base_item, "status": "conflict" if exc.status_code == 409 else "failed", "error": exc.detail})
        except Exception as exc:
            failed += 1
            results.append({**base_item, "status": "failed", "error": type(exc).__name__})

    return {
        "dry_run": dry_run,
        "strict": True,
        "hierarchy_order": STRICT_HIERARCHY_ORDER,
        "target_prefix": target_prefix,
        "review_policy": review_policy,
        "duplicate_policy": duplicate_policy,
        "rename_policy": rename_policy,
        "min_auto_score": int(min_auto_score),
        "root": str(root),
        "filesystem_pdf_count": len(pdfs),
        "database_paper_count": len(rows),
        "duplicate_file_count": duplicate_count,
        "unknown_file_count": unknown_count,
        "renamed": renamed,
        "candidates": len(results),
        "planned": planned,
        "moved": moved,
        "unchanged": unchanged,
        "skipped_review": skipped_review,
        "conflicts": conflicts,
        "failed": failed,
        "results": results[:500],
    }


def classify_unclassified_papers(
    *,
    dry_run: bool = False,
    limit: int | None = None,
    paper_ids: list[str] | None = None,
    source_paths: list[str | Path] | None = None,
    include_paper_paths: bool = True,
    include_classified: bool = False,
    include_duplicates: bool = False,
    min_auto_score: int = AUTO_MOVE_MIN_SCORE,
    strict: bool = False,
    hierarchy_order: str = STRICT_HIERARCHY_ORDER,
    target_prefix: str = STRICT_TARGET_PREFIX,
    review_policy: str = STRICT_REVIEW_POLICY,
    duplicate_policy: str = STRICT_DUPLICATE_POLICY,
    rename_policy: str = ORIGINAL_RENAME_POLICY,
) -> dict[str, Any]:
    if strict:
        return classify_library_strict(
            dry_run=dry_run,
            limit=limit,
            paper_ids=paper_ids,
            source_paths=source_paths,
            include_paper_paths=include_paper_paths,
            include_classified=include_classified,
            include_duplicates=include_duplicates,
            min_auto_score=min_auto_score,
            hierarchy_order=hierarchy_order,
            target_prefix=target_prefix,
            review_policy=review_policy,
            duplicate_policy=duplicate_policy,
            rename_policy=rename_policy,
        )

    min_auto_score = max(AUTO_CLASSIFY_MIN_SCORE, int(min_auto_score or AUTO_MOVE_MIN_SCORE))
    rows, sample_texts = _db_paper_rows(paper_ids)
    row_by_id = {row["paper_id"]: row for row in rows}
    candidates = []
    for row in rows:
        source_category = paper_files.category_path_for_file(row["file_path"])
        if source_category and not include_classified:
            continue
        decision = classify_paper_record(row, sample_texts.get(row["paper_id"], ""))
        candidates.append(
            {
                "paper_id": row["paper_id"],
                "title": row["title"],
                "source_category_path": source_category,
                "target_category_path": decision["category_path"],
                "score": decision["score"],
                "reason": decision["reason"],
            }
        )
        if limit is not None and len(candidates) >= max(1, int(limit)):
            break

    results: list[dict[str, Any]] = []
    moved = 0
    unchanged = 0
    skipped_review = 0
    conflicts = 0
    failed = 0

    for candidate in candidates:
        if candidate["source_category_path"] == candidate["target_category_path"]:
            unchanged += 1
            results.append({**candidate, "status": "unchanged"})
            continue
        source_path = Path(row_by_id[candidate["paper_id"]]["file_path"]).resolve()
        target_dir = paper_files.category_dir(candidate["target_category_path"])
        target_path = (target_dir / source_path.name).resolve()
        candidate = {
            **candidate,
            "target_path": target_path.relative_to(paper_files.papers_root()).as_posix(),
            "min_auto_score": int(min_auto_score),
        }
        if (
            candidate["score"] < int(min_auto_score)
            or candidate["target_category_path"] in {"", AUTO_FALLBACK_CATEGORY}
        ):
            skipped_review += 1
            results.append({**candidate, "status": "needs_review"})
            continue
        if source_path != target_path and target_path.exists():
            conflicts += 1
            results.append({**candidate, "status": "conflict", "error": "target_file_exists"})
            continue
        if dry_run:
            results.append({**candidate, "status": "planned"})
            continue
        try:
            move_result = paper_files.move_paper_file(
                paper_id=candidate["paper_id"],
                category_path=candidate["target_category_path"],
                create_missing_category=True,
                overwrite_existing=False,
            )
            status = move_result.get("status") or "moved"
            if status == "unchanged":
                unchanged += 1
            else:
                moved += 1
            results.append({**candidate, "status": status})
        except HTTPException as exc:
            if exc.status_code == 409 and (
                exc.detail == "target_file_exists"
                or (
                    isinstance(exc.detail, dict)
                    and exc.detail.get("code") == "target_file_exists"
                )
            ):
                conflicts += 1
                results.append({**candidate, "status": "conflict", "error": exc.detail})
            else:
                failed += 1
                results.append({**candidate, "status": "failed", "error": exc.detail})
        except Exception as exc:
            failed += 1
            results.append({**candidate, "status": "failed", "error": type(exc).__name__})

    return {
        "dry_run": dry_run,
        "strict": False,
        "min_auto_score": int(min_auto_score),
        "candidates": len(candidates),
        "moved": moved,
        "unchanged": unchanged,
        "skipped_review": skipped_review,
        "conflicts": conflicts,
        "failed": failed,
        "results": results[:300],
    }

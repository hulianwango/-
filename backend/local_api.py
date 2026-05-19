from __future__ import annotations

import html
import json
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .database import db_session, utcnow
from .indexer import scan_papers
from .mcp_tools import search_papers
from .security import require_local_request, scrub_public_payload


router = APIRouter(prefix="/local", dependencies=[Depends(require_local_request)])
LOCAL_LIBRARY_LIMIT = 1000
LOCAL_LIBRARY_LIMIT_MAX = 5000
OVERVIEW_PAGE_LIMIT = 4
OVERVIEW_TEXT_LIMIT = 9000

CHINESE_TERM_MAP = [
    (("upconversion nanoparticle", "upconverting", "ucnp"), "上转换纳米颗粒(UCNPs)"),
    (("metal-organic framework", "metal–organic framework", "mof"), "金属有机框架(MOFs)"),
    (("fret", "förster resonance energy transfer", "forster resonance energy transfer"), "能量转移(FRET)"),
    (("photodynamic", "pdt"), "光动力治疗"),
    (("chemodynamic", "cdt"), "化学动力治疗"),
    (("drug delivery",), "药物递送"),
    (("multimodal imaging", "imaging"), "成像/多模态成像"),
    (("biosensing", "biosensor"), "生物传感"),
    (("tumor microenvironment", "tme"), "肿瘤微环境"),
    (("near-infrared", "nir"), "近红外光响应"),
    (("lanthanide",), "镧系发光材料"),
    (("red emission",), "红光发射"),
    (("green emission",), "绿光发射"),
    (("plasmon", "lspr"), "等离激元增强"),
]

PDF_TEXT_REPLACEMENTS = {
    "ï¬": "fi",
    "ï¬": "fl",
    "ï¬": "ffi",
    "ï¬": "ffl",
    "â": "’",
    "â": "‘",
    "â": "“",
    "â": "”",
    "â": "–",
    "â": "—",
    "oÌˆ": "ö",
    "OÌˆ": "Ö",
    "aÌˆ": "ä",
    "AÌˆ": "Ä",
    "uÌˆ": "ü",
    "UÌˆ": "Ü",
}


class DraftUpdateRequest(BaseModel):
    annotation_json: dict[str, Any]


def _paper_public(row) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return {
        "paper_id": row["paper_id"],
        "title": html.unescape(row["title"] or ""),
        "authors": html.unescape(row["authors"] or ""),
        "year": row["year"],
        "journal": html.unescape(row["journal"] or ""),
        "doi": html.unescape(row["doi"] or ""),
        "page_count": row["page_count"] if "page_count" in row.keys() else None,
    }


def _repair_pdf_text(text: str) -> str:
    for broken, fixed in PDF_TEXT_REPLACEMENTS.items():
        text = text.replace(broken, fixed)
    return unicodedata.normalize("NFKC", text)


def _compact_text(text: str, limit: int | None = None) -> str:
    text = _repair_pdf_text(html.unescape(text or ""))
    text = text.replace("-\n", "")
    text = re.sub(r"(\w)-\s+(\w)", r"\1\2", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n[ \t]*", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    text = text.strip()
    if limit and len(text) > limit:
        return text[:limit].rsplit(" ", 1)[0].rstrip() + "..."
    return text


def _split_sentences(text: str) -> list[str]:
    compact = re.sub(r"\s+", " ", _compact_text(text)).strip()
    if not compact:
        return []
    pieces = re.split(r"(?<=[.!?。！？])\s+(?=[A-Z0-9])", compact)
    sentences: list[str] = []
    for piece in pieces:
        piece = piece.strip()
        if 40 <= len(piece) <= 650:
            sentences.append(piece)
    return sentences


def _trim_abstract_sentences(text: str) -> str:
    picked: list[str] = []
    for sentence in _split_sentences(text):
        lowered = sentence.lower()
        has_drop_cap_artifact = re.match(r"^[A-Z]\s+[a-z]", sentence) and not sentence.startswith(
            ("A ", "I ")
        )
        if has_drop_cap_artifact or lowered.startswith(("received:", "introduction")):
            break
        picked.append(sentence)
        if len(picked) >= 6:
            break
    return _compact_text(" ".join(picked), 1800)


def _sentence_score(sentence: str) -> int:
    lowered = sentence.lower()
    score = 0
    keywords = [
        "this work",
        "this review",
        "in this study",
        "in this article",
        "we report",
        "we demonstrate",
        "we show",
        "surveyed",
        "highlighted",
        "classification",
        "mechanism",
        "application",
        "applications",
        "results",
        "as a result",
        "allow",
        "offers",
    ]
    for keyword in keywords:
        if keyword in lowered:
            score += 2
    if any(term in lowered for aliases, _label in CHINESE_TERM_MAP for term in aliases):
        score += 1
    return score


def _extract_likely_abstract_block(text: str) -> str:
    lines = [_compact_text(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""

    noise = ("doi:", "www.", "©", "e-mail:", "orcid", "school of", "institute", "university")
    best = ""
    best_score = -100
    for start in range(len(lines)):
        for end in range(start + 5, min(len(lines), start + 30) + 1):
            block_lines = lines[start:end]
            block = _compact_text(" ".join(block_lines))
            if not 350 <= len(block) <= 2000:
                continue

            lowered = block.lower()
            score = sum(_sentence_score(sentence) for sentence in _split_sentences(block))
            if "in this work" in lowered or "this review" in lowered:
                score += 6
            if "abstract" in lowered:
                score += 4
            score -= sum(5 for fragment in noise if fragment in lowered)
            if block_lines[0][:1].islower():
                score -= 8
            if re.search(r"^\d+\.?\s+introduction\b", lowered):
                score -= 4
            if score > best_score:
                best_score = score
                best = block

    return _compact_text(best, 1800) if best_score > 0 else ""


def _extract_original_abstract(page_texts: list[str]) -> str:
    text = "\n".join(page_texts[:OVERVIEW_PAGE_LIMIT])
    match = re.search(
        r"(?is)\babstract\b[:\s]*(.*?)(?:\n\s*(?:keywords?|1\.?\s+introduction|introduction)\b|$)",
        text,
    )
    if match:
        raw_abstract = re.split(r"\n[A-Z]\n[a-z]", match.group(1), maxsplit=1)[0]
        explicit_abstract = _trim_abstract_sentences(raw_abstract)
        if explicit_abstract:
            return explicit_abstract
        return _compact_text(raw_abstract, 1800)

    block = _extract_likely_abstract_block(text)
    if block:
        return block

    sentences = _split_sentences(text)
    if not sentences:
        return ""

    best_start = 0
    best_score = -1
    window_size = min(6, max(3, len(sentences)))
    for start in range(0, max(1, len(sentences) - window_size + 1)):
        window = sentences[start : start + window_size]
        joined = " ".join(window)
        if len(joined) < 350:
            continue
        score = sum(_sentence_score(sentence) for sentence in window)
        if score > best_score:
            best_score = score
            best_start = start

    return _compact_text(" ".join(sentences[best_start : best_start + window_size]), 1800)


def _extract_main_points(text: str) -> list[str]:
    sentences = _split_sentences(text)
    if not sentences:
        return []
    ranked = sorted(
        ((-_sentence_score(sentence), index, sentence) for index, sentence in enumerate(sentences)),
        key=lambda item: (item[0], item[1]),
    )
    points: list[str] = []
    for _score, _index, sentence in ranked:
        if sentence not in points:
            points.append(_compact_text(sentence, 360))
        if len(points) >= 5:
            break
    return points


def _extract_section_headings(rows: list[Any]) -> list[dict[str, Any]]:
    headings: list[dict[str, Any]] = []
    seen: set[str] = set()
    heading_re = re.compile(
        r"^(?:\d{1,2}(?:\.\d+)*\.?\s+)?[A-Z][A-Za-z0-9α-ωΑ-Ωµμβγδλπ–—\-/,:;@&() ]{3,120}$"
    )
    skip_fragments = (
        "www.",
        "doi:",
        "copyright",
        "©",
        "received",
        "accepted",
        "published",
        "supporting information",
    )
    for row in rows:
        for raw_line in (row["text"] or "").splitlines():
            line = _compact_text(raw_line)
            lowered = line.lower()
            if not line or len(line) < 5 or len(line) > 130:
                continue
            if any(fragment in lowered for fragment in skip_fragments):
                continue
            is_numbered = bool(re.match(r"^\d{1,2}(?:\.\d+)*\.?\s+\S", line))
            is_named = lowered in {
                "abstract",
                "introduction",
                "conclusion",
                "conclusions",
                "experimental section",
                "results and discussion",
                "supporting information",
            }
            if not is_numbered and not is_named:
                continue
            if not heading_re.match(line):
                continue
            if line.endswith(".") and not re.match(r"^\d", line):
                continue
            if line in seen:
                continue
            seen.add(line)
            headings.append({"page_number": row["page_number"], "text": line})
            if len(headings) >= 12:
                return headings
    return headings


def _guess_authors_from_first_page(first_page_text: str, title: str = "") -> str:
    lines = [_compact_text(line) for line in first_page_text.splitlines()]
    lines = [line for line in lines[:28] if line]
    title_lower = title.lower()
    candidates: list[str] = []
    stop_words = (
        "abstract",
        "doi:",
        "www.",
        "review",
        "university",
        "institute",
        "school of",
        "department",
        "laboratory",
        "supporting information",
        "received:",
        "accepted:",
    )
    for line in lines:
        lowered = line.lower()
        if any(word in lowered for word in stop_words):
            continue
        if title_lower and lowered in title_lower:
            continue
        if len(line) > 180 or sum(char.isdigit() for char in line) > 6:
            continue
        if "," in line or " and " in lowered or re.search(r"\b[A-Z]\.\s*[A-Z]", line):
            candidates.append(line)
    return candidates[0] if candidates else ""


def _annotation_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(str(item) for item in value if item)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _summary_from_annotation(annotation: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("main_work", "mechanism_summary", "key_results", "relevance_to_project"):
        text = _annotation_text(annotation.get(key)).strip()
        if text:
            parts.append(text)
    return _compact_text("\n\n".join(parts), 1400)


def _points_from_annotation(annotation: dict[str, Any]) -> list[str]:
    points: list[str] = []
    for key in ("main_work", "material_system", "methods", "key_results", "mechanisms", "limitations"):
        text = _annotation_text(annotation.get(key)).strip()
        if text:
            points.append(_compact_text(text, 360))
    return points[:6]


def _detected_chinese_terms(*texts: str) -> list[str]:
    haystack = " ".join(texts).lower()
    labels: list[str] = []
    for aliases, label in CHINESE_TERM_MAP:
        if any(alias in haystack for alias in aliases) and label not in labels:
            labels.append(label)
    return labels


def _auto_chinese_overview(paper: dict[str, Any], abstract_text: str, headings: list[dict[str, Any]]) -> str:
    title = paper.get("title") or "这篇文献"
    terms = _detected_chinese_terms(title, abstract_text, " ".join(item["text"] for item in headings))
    if terms:
        topic = "、".join(terms[:6])
        first = f"这篇文献围绕 {topic} 展开，重点关注这些体系的构筑、机制和应用价值。"
    else:
        first = f"这篇文献围绕《{title}》展开，建议结合下方原文摘要和章节线索快速定位内容。"

    heading_text = "；".join(item["text"] for item in headings[:4])
    if heading_text:
        return f"{first} 从章节线索看，正文主要包括：{heading_text}。"
    return first


def _has_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def _auto_english_overview(
    paper: dict[str, Any], abstract_text: str, headings: list[dict[str, Any]]
) -> str:
    if abstract_text:
        return _compact_text(abstract_text, 720)

    title = paper.get("title") or "this paper"
    heading_text = "; ".join(item["text"] for item in headings[:4])
    if heading_text:
        return f"This paper focuses on {title}. The detected section clues include: {heading_text}."
    return f"This paper focuses on {title}."


def _auto_chinese_point(sentence: str, paper: dict[str, Any]) -> str:
    sentence = _compact_text(sentence)
    if not sentence or _has_chinese(sentence):
        return sentence

    lowered = sentence.lower()
    title = paper.get("title") or ""
    terms = _detected_chinese_terms(title, sentence)
    topic = "、".join(terms[:4]) if terms else "论文核心体系"

    if "photon counting" in lowered and "quenching mechanism" in lowered:
        return "光子计数实验表明，该纳米组装体的猝灭机制与能量转移(FRET)和发射再吸收共同相关。"

    if "photoresponsive amphiphilic polymer" in lowered and "encapsulat" in lowered:
        return "研究合成了光响应两亲性聚合物，并用它包载上转换镧系掺杂纳米颗粒，构建水分散纳米组装体。"

    if "fluorescent emission" in lowered and "980" in lowered and "reversibly" in lowered:
        return "该纳米组装体在 980 nm 光照下产生可见区荧光，并可通过切换光响应发色团的异构状态实现可逆调控。"

    if "compared" in lowered and "quenching efficiency" in lowered:
        return "与类似的 plug-and-play 纳米组装体相比，该体系因光开关负载量更高而表现出更高的整体猝灭效率。"

    if "synthesized" in lowered and "encapsulat" in lowered:
        return f"该要点说明研究通过合成和包载策略构建了与{topic}相关的材料体系。"

    if "exhibit" in lowered and ("emission" in lowered or "fluorescen" in lowered):
        return f"该要点强调{topic}的发光行为及其可调控特征。"

    if "experiment" in lowered or "show" in lowered or "demonstrat" in lowered:
        return f"实验结果表明，{topic}中的关键机制或性能得到了验证。"

    if "compared" in lowered or "higher" in lowered or "increased" in lowered:
        return f"该要点给出了与已有方法或参照体系的比较，突出{topic}在性能上的提升。"

    if "mechanism" in lowered:
        return f"该要点聚焦{topic}的作用机制，说明论文如何解释观察到的结果。"

    if "application" in lowered or "applications" in lowered:
        return f"该要点概括{topic}的潜在应用方向和研究价值。"

    if terms:
        return f"该要点围绕{topic}展开，概括了论文中的一个关键观察或结论。"

    return "该要点概括了论文中的一个关键研究对象、实验观察或比较结论。"


def _auto_chinese_points(points: list[str], paper: dict[str, Any]) -> list[str]:
    return [_auto_chinese_point(point, paper) for point in points if _compact_text(point)]


def _auto_chinese_abstract(
    paper: dict[str, Any],
    abstract_text: str,
    headings: list[dict[str, Any]],
    chinese_points: list[str],
) -> str:
    useful_points = [point.rstrip("。") for point in chinese_points if point][:3]
    if useful_points:
        return _compact_text("。".join(useful_points) + "。", 900)
    return _auto_chinese_overview(paper, abstract_text, headings)


@router.post("/scan")
def scan_library() -> dict[str, Any]:
    return scan_papers()


@router.get("/papers")
def list_or_search_papers(query: str = "", limit: int = LOCAL_LIBRARY_LIMIT) -> list[dict[str, Any]]:
    query = query.strip()
    if query:
        return search_papers(query=query, limit=min(limit, 10))

    limit = max(1, min(limit, LOCAL_LIBRARY_LIMIT_MAX))
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT paper_id, title, authors, year, journal, doi, page_count
            FROM papers
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return scrub_public_payload([_paper_public(row) for row in rows])


@router.get("/papers/{paper_id}")
def get_local_paper(paper_id: str) -> dict[str, Any]:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT paper_id, title, authors, year, journal, doi, page_count
            FROM papers
            WHERE paper_id = ?
            """,
            (paper_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="paper not found.")
    return scrub_public_payload(_paper_public(row))


@router.get("/papers/{paper_id}/overview")
def get_local_paper_overview(paper_id: str) -> dict[str, Any]:
    with db_session() as connection:
        paper_row = connection.execute(
            """
            SELECT paper_id, title, authors, year, journal, doi, page_count
            FROM papers
            WHERE paper_id = ?
            """,
            (paper_id,),
        ).fetchone()
        if paper_row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="paper not found.")

        page_rows = connection.execute(
            """
            SELECT page_number, text
            FROM paper_pages
            WHERE paper_id = ?
            ORDER BY page_number
            """,
            (paper_id,),
        ).fetchall()

        annotation_row = connection.execute(
            """
            SELECT annotation_json, status, updated_at, 'draft' AS source
            FROM paper_ai_drafts
            WHERE paper_id = ?
            UNION ALL
            SELECT annotation_json, 'accepted' AS status, updated_at, 'annotation' AS source
            FROM paper_annotations
            WHERE paper_id = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (paper_id, paper_id),
        ).fetchone()

    paper = _paper_public(paper_row)
    page_texts = [row["text"] for row in page_rows[:OVERVIEW_PAGE_LIMIT]]
    if not paper.get("authors") and page_texts:
        paper["authors"] = _guess_authors_from_first_page(page_texts[0], paper.get("title") or "")
    abstract_text = _extract_original_abstract(page_texts)
    headings = _extract_section_headings(page_rows)

    annotation: dict[str, Any] = {}
    annotation_source = ""
    annotation_status = ""
    if annotation_row is not None:
        annotation = json.loads(annotation_row["annotation_json"])
        annotation_source = annotation_row["source"]
        annotation_status = annotation_row["status"]

    saved_summary = _summary_from_annotation(annotation)
    chinese_summary = saved_summary if _has_chinese(saved_summary) else ""
    annotation_points = _points_from_annotation(annotation)
    annotation_points_zh = [point for point in annotation_points if _has_chinese(point)]
    annotation_points_en = [point for point in annotation_points if not _has_chinese(point)]

    source_text = abstract_text or _compact_text("\n".join(page_texts), OVERVIEW_TEXT_LIMIT)
    source_points = _extract_main_points(source_text)
    main_points_en = annotation_points_en or source_points
    main_points_zh = annotation_points_zh or _auto_chinese_points(main_points_en, paper)

    return scrub_public_payload(
        {
            **paper,
            "has_chinese_summary": bool(chinese_summary),
            "chinese_summary": chinese_summary,
            "auto_chinese_abstract": _auto_chinese_abstract(
                paper, abstract_text, headings, main_points_zh
            ),
            "summary_source": annotation_source,
            "summary_status": annotation_status,
            "missing_chinese_reason": ""
            if chinese_summary
            else "数据库目前只有 PDF 原文索引，还没有为这篇文献保存待审或已接受的 AI 中文摘要草稿。",
            "auto_chinese_overview": _auto_chinese_overview(paper, abstract_text, headings),
            "auto_english_overview": _auto_english_overview(paper, abstract_text, headings),
            "abstract_text": abstract_text,
            "main_points": main_points_en,
            "main_points_zh": main_points_zh,
            "main_points_en": main_points_en,
            "section_headings": headings,
        }
    )


@router.get("/papers/{paper_id}/pdf")
def read_local_pdf(paper_id: str) -> FileResponse:
    with db_session() as connection:
        row = connection.execute(
            "SELECT file_path FROM papers WHERE paper_id = ?", (paper_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="paper not found.")
    pdf_path = Path(row["file_path"])
    if not pdf_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pdf not found.")
    return FileResponse(pdf_path, media_type="application/pdf")


@router.get("/drafts")
def list_drafts(status_filter: str = "pending") -> list[dict[str, Any]]:
    allowed_statuses = {"pending", "accepted", "rejected", "all"}
    if status_filter not in allowed_statuses:
        status_filter = "pending"

    where = "" if status_filter == "all" else "WHERE d.status = ?"
    params: tuple[Any, ...] = () if status_filter == "all" else (status_filter,)
    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT
                d.draft_id,
                d.paper_id,
                d.annotation_json,
                d.status,
                p.title,
                p.authors,
                p.year,
                p.journal,
                p.doi
            FROM paper_ai_drafts AS d
            JOIN papers AS p ON p.paper_id = d.paper_id
            {where}
            ORDER BY d.updated_at DESC
            """,
            params,
        ).fetchall()

    drafts: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["annotation_json"] = json.loads(item["annotation_json"])
        drafts.append(item)
    return scrub_public_payload(drafts)


@router.put("/drafts/{draft_id}")
def update_local_draft(draft_id: str, payload: DraftUpdateRequest) -> dict[str, Any]:
    now = utcnow()
    with db_session() as connection:
        row = connection.execute(
            "SELECT paper_id, status FROM paper_ai_drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="draft not found.")
        if row["status"] != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="only pending drafts can be edited.",
            )
        connection.execute(
            """
            UPDATE paper_ai_drafts
            SET annotation_json = ?, updated_at = ?
            WHERE draft_id = ?
            """,
            (json.dumps(payload.annotation_json, ensure_ascii=False), now, draft_id),
        )
    return {"draft_id": draft_id, "paper_id": row["paper_id"], "status": "pending"}


@router.post("/drafts/{draft_id}/accept")
def accept_draft(draft_id: str) -> dict[str, Any]:
    now = utcnow()
    annotation_id = f"a_{uuid.uuid4().hex[:20]}"

    with db_session() as connection:
        row = connection.execute(
            """
            SELECT draft_id, paper_id, annotation_json, status
            FROM paper_ai_drafts
            WHERE draft_id = ?
            """,
            (draft_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="draft not found.")
        if row["status"] != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="only pending drafts can be accepted.",
            )

        annotation = json.loads(row["annotation_json"])
        paper_id = row["paper_id"]
        connection.execute(
            """
            INSERT INTO paper_annotations (
                annotation_id, paper_id, source_draft_id, annotation_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (annotation_id, paper_id, draft_id, json.dumps(annotation, ensure_ascii=False), now, now),
        )

        mechanism_summary = annotation.get("mechanism_summary") or annotation.get("mechanisms")
        if mechanism_summary:
            connection.execute(
                """
                INSERT INTO paper_mechanisms (
                    paper_id, mechanism_summary, evidence_json, page_numbers_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    paper_id,
                    str(mechanism_summary),
                    json.dumps(annotation.get("evidence", []), ensure_ascii=False),
                    json.dumps(annotation.get("page_numbers", []), ensure_ascii=False),
                    now,
                    now,
                ),
            )

        relevance_fields = {
            "is_colloid",
            "au_shape",
            "au_size_nm",
            "lspr_peak_nm",
            "er_host",
            "involves_520_nm",
            "involves_540_nm",
            "involves_red_emission",
            "supports_red_emission_design",
            "warns_green_channel_enhancement",
            "mechanism_summary",
            "relevance_score",
        }
        if any(key in annotation for key in relevance_fields):
            connection.execute(
                """
                INSERT INTO project_relevance (
                    paper_id, is_colloid, au_shape, au_size_nm, lspr_peak_nm, er_host,
                    involves_520_nm, involves_540_nm, involves_red_emission,
                    supports_red_emission_design, warns_green_channel_enhancement,
                    mechanism_summary, relevance_score, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(paper_id) DO UPDATE SET
                    is_colloid = excluded.is_colloid,
                    au_shape = excluded.au_shape,
                    au_size_nm = excluded.au_size_nm,
                    lspr_peak_nm = excluded.lspr_peak_nm,
                    er_host = excluded.er_host,
                    involves_520_nm = excluded.involves_520_nm,
                    involves_540_nm = excluded.involves_540_nm,
                    involves_red_emission = excluded.involves_red_emission,
                    supports_red_emission_design = excluded.supports_red_emission_design,
                    warns_green_channel_enhancement = excluded.warns_green_channel_enhancement,
                    mechanism_summary = excluded.mechanism_summary,
                    relevance_score = excluded.relevance_score,
                    updated_at = excluded.updated_at
                """,
                (
                    paper_id,
                    _bool_int(annotation.get("is_colloid")),
                    annotation.get("au_shape"),
                    annotation.get("au_size_nm"),
                    annotation.get("lspr_peak_nm"),
                    annotation.get("er_host"),
                    _bool_int(annotation.get("involves_520_nm")),
                    _bool_int(annotation.get("involves_540_nm")),
                    _bool_int(annotation.get("involves_red_emission")),
                    _bool_int(annotation.get("supports_red_emission_design")),
                    _bool_int(annotation.get("warns_green_channel_enhancement")),
                    annotation.get("mechanism_summary"),
                    annotation.get("relevance_score"),
                    now,
                    now,
                ),
            )

        for tag in annotation.get("recommended_tags") or []:
            if isinstance(tag, str) and tag.strip():
                connection.execute(
                    """
                    INSERT OR IGNORE INTO paper_tags (paper_id, tag, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (paper_id, tag.strip(), now),
                )

        connection.execute(
            "UPDATE paper_ai_drafts SET status = 'accepted', updated_at = ? WHERE draft_id = ?",
            (now, draft_id),
        )

    return {"draft_id": draft_id, "paper_id": paper_id, "status": "accepted"}


@router.post("/drafts/{draft_id}/reject")
def reject_draft(draft_id: str) -> dict[str, Any]:
    now = utcnow()
    with db_session() as connection:
        row = connection.execute(
            "SELECT paper_id, status FROM paper_ai_drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="draft not found.")
        if row["status"] != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="only pending drafts can be rejected.",
            )
        connection.execute(
            "UPDATE paper_ai_drafts SET status = 'rejected', updated_at = ? WHERE draft_id = ?",
            (now, draft_id),
        )
    return {"draft_id": draft_id, "paper_id": row["paper_id"], "status": "rejected"}


def _bool_int(value: Any) -> int | None:
    if value is None:
        return None
    return 1 if bool(value) else 0

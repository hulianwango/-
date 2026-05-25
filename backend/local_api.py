from __future__ import annotations

import html
import json
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from . import paper_files
from .auto_classifier import classify_unclassified_papers
from .database import db_session, utcnow
from .indexer import scan_papers
from .mcp_tools import search_papers
from .security import require_local_request, scrub_public_payload


router = APIRouter(prefix="/local", dependencies=[Depends(require_local_request)])
LOCAL_LIBRARY_LIMIT = 1000
LOCAL_LIBRARY_LIMIT_MAX = 5000
OVERVIEW_PAGE_LIMIT = 4
OVERVIEW_TEXT_LIMIT = 9000
ALL_CATEGORIES = "__all__"
UNTAGGED_TAG_FILTER = "__untagged__"
TAG_MAX_LENGTH = 40
TAGS_PER_PAPER_MAX = 20
READ_STATUSES = {"unread", "reading", "read"}

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


class PaperMoveRequest(BaseModel):
    category_path: str
    create_missing_category: bool = True
    overwrite_existing: bool = False


class PaperTagsRequest(BaseModel):
    tags: list[str]


class PaperMetadataRequest(BaseModel):
    title: str
    authors: str = ""
    year: int | None = None
    journal: str = ""
    doi: str = ""


class PaperReadingStateRequest(BaseModel):
    read_status: str = "unread"
    is_favorite: bool = False
    is_later: bool = False


class AutoClassifyRequest(BaseModel):
    dry_run: bool = False
    limit: int | None = None
    include_classified: bool = True
    include_duplicates: bool = True
    min_auto_score: int = 8
    strict: bool = True
    hierarchy_order: str = "mechanism/material_structure/application"
    target_prefix: str = ""
    review_policy: str = "review"
    duplicate_policy: str = "classify"
    rename_policy: str = "original"


class EmptyCategoryDeleteRequest(BaseModel):
    category_path: str


def _row_has(row, key: str) -> bool:  # type: ignore[no-untyped-def]
    return key in row.keys()


def _clean_tag(raw_tag: str) -> str:
    tag = re.sub(r"\s+", " ", str(raw_tag or "")).strip()
    if not tag:
        return ""
    if tag == UNTAGGED_TAG_FILTER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="tag value is reserved for filtering.",
        )
    if len(tag) > TAG_MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"tag must be at most {TAG_MAX_LENGTH} characters.",
        )
    if any(char in tag for char in "\r\n\t,"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="tag cannot contain commas or line breaks.",
        )
    return tag


def _clean_tag_filter(raw_tag: str) -> str:
    tag = str(raw_tag or "").strip()
    if tag == UNTAGGED_TAG_FILTER:
        return tag
    return _clean_tag(tag) if tag else ""


def _clean_tags(raw_tags: list[str]) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for raw_tag in raw_tags:
        tag = _clean_tag(raw_tag)
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
        if len(tags) > TAGS_PER_PAPER_MAX:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"each paper can have at most {TAGS_PER_PAPER_MAX} tags.",
            )
    return tags


def _clean_recommended_tags(raw_tags: list[Any]) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for raw_tag in raw_tags:
        if not isinstance(raw_tag, str):
            continue
        try:
            tag = _clean_tag(raw_tag)
        except HTTPException:
            continue
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
        if len(tags) >= TAGS_PER_PAPER_MAX:
            break
    return tags


def _clean_paper_metadata(payload: PaperMetadataRequest) -> dict[str, Any]:
    title = html.unescape(payload.title or "").strip()
    if not title:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="title is required.")
    year = payload.year
    if year is not None and not 1500 <= int(year) <= 2100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="year must be between 1500 and 2100.",
        )
    return {
        "title": title,
        "authors": html.unescape(payload.authors or "").strip(),
        "year": year,
        "journal": html.unescape(payload.journal or "").strip(),
        "doi": html.unescape(payload.doi or "").strip(),
    }


def _default_reading_state() -> dict[str, Any]:
    return {"read_status": "unread", "is_favorite": False, "is_later": False}


def _clean_reading_state(payload: PaperReadingStateRequest) -> dict[str, Any]:
    read_status = (payload.read_status or "unread").strip()
    if read_status not in READ_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="read_status must be unread, reading, or read.",
        )
    return {
        "read_status": read_status,
        "is_favorite": bool(payload.is_favorite),
        "is_later": bool(payload.is_later),
    }


def _tags_by_paper_ids(connection, paper_ids: list[str]) -> dict[str, list[str]]:  # type: ignore[no-untyped-def]
    if not paper_ids:
        return {}
    unique_ids = list(dict.fromkeys(paper_ids))
    placeholders = ",".join("?" for _ in unique_ids)
    rows = connection.execute(
        f"""
        SELECT paper_id, tag
        FROM paper_tags
        WHERE paper_id IN ({placeholders})
        ORDER BY lower(tag), tag
        """,
        tuple(unique_ids),
    ).fetchall()
    tags: dict[str, list[str]] = {paper_id: [] for paper_id in unique_ids}
    for row in rows:
        tags.setdefault(row["paper_id"], []).append(row["tag"])
    return tags


def _reading_states_by_paper_ids(
    connection,
    paper_ids: list[str],
) -> dict[str, dict[str, Any]]:  # type: ignore[no-untyped-def]
    if not paper_ids:
        return {}
    unique_ids = list(dict.fromkeys(paper_ids))
    placeholders = ",".join("?" for _ in unique_ids)
    rows = connection.execute(
        f"""
        SELECT paper_id, read_status, is_favorite, is_later
        FROM paper_reading_states
        WHERE paper_id IN ({placeholders})
        """,
        tuple(unique_ids),
    ).fetchall()
    states = {paper_id: _default_reading_state() for paper_id in unique_ids}
    for row in rows:
        states[row["paper_id"]] = {
            "read_status": row["read_status"],
            "is_favorite": bool(row["is_favorite"]),
            "is_later": bool(row["is_later"]),
        }
    return states


def _paper_exists(connection, paper_id: str) -> bool:  # type: ignore[no-untyped-def]
    row = connection.execute(
        "SELECT 1 FROM papers WHERE paper_id = ? LIMIT 1",
        (paper_id,),
    ).fetchone()
    return row is not None


def _paper_public(
    row,
    tags: list[str] | None = None,
    reading_state: dict[str, Any] | None = None,
) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    file_path = row["file_path"] if _row_has(row, "file_path") else ""
    return {
        "paper_id": row["paper_id"],
        "title": html.unescape(row["title"] or ""),
        "authors": html.unescape(row["authors"] or ""),
        "year": row["year"],
        "journal": html.unescape(row["journal"] or ""),
        "doi": html.unescape(row["doi"] or ""),
        "page_count": row["page_count"] if _row_has(row, "page_count") else None,
        "imported_at": row["created_at"] if _row_has(row, "created_at") else "",
        "updated_at": row["updated_at"] if _row_has(row, "updated_at") else "",
        "tags": tags or [],
        "reading_state": reading_state or _default_reading_state(),
        **paper_files.paper_category_fields(file_path),
    }


def _paper_matches_category(paper: dict[str, Any], category_path: str | None) -> bool:
    return category_path is None or paper.get("category_path", "") == category_path


def _paper_matches_filters(
    paper: dict[str, Any],
    category_path: str | None,
    tag: str,
) -> bool:
    if not _paper_matches_category(paper, category_path):
        return False
    paper_tags = paper.get("tags") or []
    if tag == UNTAGGED_TAG_FILTER:
        return not paper_tags
    if tag and tag not in paper_tags:
        return False
    return True


def _reference_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _split_reference_authors(authors: str) -> list[str]:
    text = _reference_text(authors)
    if not text:
        return []
    parts = re.split(r"\s*(?:;|；|\band\b|&)\s*", text)
    parts = [_reference_text(part) for part in parts if _reference_text(part)]
    return parts or [text]


def _bibtex_escape(value: Any) -> str:
    text = _reference_text(value)
    return text.replace("\\", "\\textbackslash{}").replace("{", "\\{").replace("}", "\\}")


def _ascii_words(value: Any) -> list[str]:
    normalized = unicodedata.normalize("NFKD", _reference_text(value))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.findall(r"[A-Za-z0-9]+", ascii_text)


def _citation_key(paper: dict[str, Any], used: set[str]) -> str:
    authors = _split_reference_authors(paper.get("authors") or "")
    author_words = _ascii_words(authors[0] if authors else "")
    title_words = _ascii_words(paper.get("title"))[:3]
    author = author_words[-1] if author_words else "paper"
    year = str(paper.get("year") or "nd")
    base = "".join([author.lower(), year, *(word[:12].lower() for word in title_words)])
    base = re.sub(r"[^a-z0-9]+", "", base) or f"paper{str(paper.get('paper_id') or '')[:8]}"
    key = base
    index = 2
    while key in used:
        key = f"{base}{index}"
        index += 1
    used.add(key)
    return key


def _reference_rows(
    *,
    category_path: str = ALL_CATEGORIES,
    tag: str = "",
    paper_ids: str = "",
) -> list[dict[str, Any]]:
    requested_ids = [item.strip() for item in str(paper_ids or "").split(",") if item.strip()]
    category_filter = (
        None if category_path == ALL_CATEGORIES else paper_files.normalize_category_path(category_path)
    )
    tag_filter = _clean_tag_filter(tag)

    where = ""
    params: tuple[Any, ...] = ()
    if requested_ids:
        unique_ids = list(dict.fromkeys(requested_ids))
        placeholders = ",".join("?" for _ in unique_ids)
        where = f"WHERE paper_id IN ({placeholders})"
        params = tuple(unique_ids)

    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT
                paper_id, title, authors, year, journal, doi, file_path,
                page_count, created_at, updated_at
            FROM papers
            {where}
            ORDER BY authors COLLATE NOCASE, year, title COLLATE NOCASE
            """,
            params,
        ).fetchall()
        tag_map = _tags_by_paper_ids(connection, [row["paper_id"] for row in rows])

    papers = [
        _paper_public(
            row,
            tags=tag_map.get(row["paper_id"], []),
        )
        for row in rows
    ]
    return [
        paper
        for paper in papers
        if _paper_matches_filters(paper, category_filter, tag_filter)
    ]


def _format_bibtex_references(papers: list[dict[str, Any]]) -> str:
    used: set[str] = set()
    entries: list[str] = []
    for paper in papers:
        key = _citation_key(paper, used)
        fields = {
            "title": paper.get("title"),
            "author": " and ".join(_split_reference_authors(paper.get("authors") or "")),
            "year": paper.get("year"),
            "journal": paper.get("journal"),
            "doi": paper.get("doi"),
        }
        field_lines = [
            f"  {name} = {{{_bibtex_escape(value)}}},"
            for name, value in fields.items()
            if _reference_text(value)
        ]
        entry_type = "article" if _reference_text(paper.get("journal")) else "misc"
        entries.append("@%s{%s,\n%s\n}" % (entry_type, key, "\n".join(field_lines)))
    return "\n\n".join(entries) + ("\n" if entries else "")


def _format_ris_references(papers: list[dict[str, Any]]) -> str:
    records: list[str] = []
    for paper in papers:
        lines = ["TY  - JOUR" if _reference_text(paper.get("journal")) else "TY  - GEN"]
        for author in _split_reference_authors(paper.get("authors") or ""):
            lines.append(f"AU  - {author}")
        if _reference_text(paper.get("title")):
            lines.append(f"TI  - {_reference_text(paper.get('title'))}")
        if _reference_text(paper.get("journal")):
            lines.append(f"JO  - {_reference_text(paper.get('journal'))}")
        if paper.get("year"):
            lines.append(f"PY  - {paper['year']}")
        if _reference_text(paper.get("doi")):
            doi = _reference_text(paper.get("doi"))
            lines.append(f"DO  - {doi}")
            lines.append(f"UR  - https://doi.org/{doi}")
        lines.append("ER  -")
        records.append("\n".join(lines))
    return "\n\n".join(records) + ("\n" if records else "")


def _format_text_references(papers: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, paper in enumerate(papers, start=1):
        authors = _reference_text(paper.get("authors")) or "Unknown authors"
        title = _reference_text(paper.get("title")) or "Untitled"
        journal = _reference_text(paper.get("journal"))
        year = str(paper.get("year") or "n.d.")
        doi = _reference_text(paper.get("doi"))
        citation = f"[{index}] {authors}. {title}"
        if journal:
            citation += f"[J]. {journal}, {year}."
        else:
            citation += f". {year}."
        if doi:
            citation += f" DOI: {doi}."
        lines.append(citation)
    return "\n".join(lines) + ("\n" if lines else "")


def _reference_export_payload(
    papers: list[dict[str, Any]],
    export_format: str,
) -> tuple[str, str, str]:
    normalized = (export_format or "bibtex").strip().lower()
    if normalized in {"bib", "bibtex"}:
        return _format_bibtex_references(papers), "references.bib", "application/x-bibtex; charset=utf-8"
    if normalized == "ris":
        return _format_ris_references(papers), "references.ris", "application/x-research-info-systems; charset=utf-8"
    if normalized in {"txt", "text", "gbt"}:
        return _format_text_references(papers), "references.txt", "text/plain; charset=utf-8"
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="format must be bibtex, ris, or text.",
    )


def _local_fts_query(query: str) -> str:
    tokens = re.findall(r"[\w\u4e00-\u9fff-]+", query, flags=re.UNICODE)
    if not tokens:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="query is required.")
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens[:12])


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


def _page_search_snippet(text: str, query: str, limit: int = 280) -> str:
    compact = _compact_text(text)
    if not compact:
        return ""
    index = compact.casefold().find(query.casefold())
    if index < 0:
        return _compact_text(compact, limit)
    start = max(0, index - limit // 2)
    end = min(len(compact), index + len(query) + limit // 2)
    snippet = compact[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(compact):
        snippet = snippet + "..."
    return snippet


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


def _draft_from_indexed_text(
    paper: dict[str, Any],
    page_rows: list[Any],
) -> dict[str, Any]:
    page_texts = [row["text"] for row in page_rows[:OVERVIEW_PAGE_LIMIT]]
    abstract_text = _extract_original_abstract(page_texts)
    headings = _extract_section_headings(page_rows)
    source_text = abstract_text or _compact_text("\n".join(page_texts), OVERVIEW_TEXT_LIMIT)
    main_points_en = _extract_main_points(source_text)
    main_points_zh = _auto_chinese_points(main_points_en, paper)
    summary = _auto_chinese_abstract(paper, abstract_text, headings, main_points_zh)
    recommended_tags = _detected_chinese_terms(
        paper.get("title") or "",
        abstract_text,
        " ".join(main_points_en),
    )
    page_numbers = sorted({item["page_number"] for item in headings[:6]}) or [
        row["page_number"] for row in page_rows[: min(OVERVIEW_PAGE_LIMIT, len(page_rows))]
    ]
    return {
        "main_work": summary,
        "material_system": paper.get("title") or "",
        "methods": "",
        "key_results": main_points_zh or main_points_en,
        "mechanisms": "",
        "evidence": [
            {"page_number": item["page_number"], "text": item["text"]}
            for item in headings[:6]
        ],
        "page_numbers": page_numbers,
        "relevance_to_project": "",
        "recommended_tags": recommended_tags,
        "limitations": "自动生成的待审草稿，需要结合 PDF 原文核对后再接受。",
        "mechanism_summary": _auto_chinese_overview(paper, abstract_text, headings),
        "confidence": 0.45,
    }


def _quality_issue(kind: str, label: str, paper: dict[str, Any], detail: str = "") -> dict[str, Any]:
    return {
        "kind": kind,
        "label": label,
        "paper_id": paper["paper_id"],
        "title": paper.get("title") or "Untitled",
        "detail": detail,
    }


def _merge_tag_suggestion(
    suggestions: dict[str, dict[str, Any]],
    *,
    tag: str,
    paper_count: int,
    source: str,
) -> None:
    try:
        clean = _clean_tag(tag)
    except HTTPException:
        return
    if not clean:
        return
    key = clean.casefold()
    existing = suggestions.get(key)
    if existing is None:
        suggestions[key] = {"tag": clean, "paper_count": int(paper_count or 0), "source": source}
        return

    existing["paper_count"] = max(int(existing.get("paper_count") or 0), int(paper_count or 0))
    sources = str(existing.get("source") or "").split("+")
    if source not in sources:
        sources.append(source)
        existing["source"] = "+".join(source for source in sources if source)


def _category_tag_segments(category_path: str) -> list[str]:
    segments: list[str] = []
    seen: set[str] = set()
    for raw_segment in str(category_path or "").split("/"):
        segment = html.unescape(raw_segment).strip()
        if not segment:
            continue
        try:
            clean = _clean_tag(segment)
        except HTTPException:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        segments.append(clean)
    return segments


@router.post("/scan")
def scan_library() -> dict[str, Any]:
    return scan_papers()


@router.post("/auto-classify")
def auto_classify_library(payload: AutoClassifyRequest = AutoClassifyRequest()) -> dict[str, Any]:
    return scrub_public_payload(
        classify_unclassified_papers(
            dry_run=payload.dry_run,
            limit=payload.limit,
            include_classified=payload.include_classified,
            include_duplicates=payload.include_duplicates,
            min_auto_score=payload.min_auto_score,
            strict=payload.strict,
            hierarchy_order=payload.hierarchy_order,
            target_prefix=payload.target_prefix,
            review_policy=payload.review_policy,
            duplicate_policy=payload.duplicate_policy,
            rename_policy=payload.rename_policy,
        )
    )


@router.get("/references/export")
def export_local_references(
    format: str = "bibtex",
    category_path: str = ALL_CATEGORIES,
    tag: str = "",
    paper_ids: str = "",
) -> Response:
    papers = _reference_rows(category_path=category_path, tag=tag, paper_ids=paper_ids)
    content, filename, media_type = _reference_export_payload(papers, format)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/categories")
def list_local_categories(include_empty: bool = True) -> list[dict[str, Any]]:
    return scrub_public_payload(paper_files.list_paper_categories(include_empty=include_empty))


@router.get("/empty-categories")
def list_local_empty_categories() -> list[dict[str, Any]]:
    return scrub_public_payload(paper_files.list_empty_categories())


@router.post("/empty-categories/delete")
def delete_local_empty_category(payload: EmptyCategoryDeleteRequest) -> dict[str, Any]:
    return scrub_public_payload(paper_files.delete_empty_category(payload.category_path))


@router.get("/tags")
def list_local_tags() -> list[dict[str, Any]]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT tag, COUNT(*) AS paper_count
            FROM paper_tags
            GROUP BY tag
            ORDER BY lower(tag), tag
            """
        ).fetchall()
    return scrub_public_payload([dict(row) for row in rows])


@router.get("/tag-suggestions")
def list_local_tag_suggestions(limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 100), 500))
    suggestions: dict[str, dict[str, Any]] = {}
    category_papers: dict[str, set[str]] = {}

    with db_session() as connection:
        tag_rows = connection.execute(
            """
            SELECT tag, COUNT(*) AS paper_count
            FROM paper_tags
            GROUP BY tag
            ORDER BY lower(tag), tag
            """
        ).fetchall()
        paper_rows = connection.execute(
            """
            SELECT paper_id, file_path
            FROM papers
            ORDER BY updated_at DESC
            """
        ).fetchall()

    for row in tag_rows:
        _merge_tag_suggestion(
            suggestions,
            tag=row["tag"],
            paper_count=int(row["paper_count"] or 0),
            source="saved",
        )

    for row in paper_rows:
        category_path = paper_files.category_path_for_file(row["file_path"])
        for tag in _category_tag_segments(category_path):
            category_papers.setdefault(tag.casefold(), set()).add(row["paper_id"])
            if tag.casefold() not in suggestions:
                suggestions[tag.casefold()] = {"tag": tag, "paper_count": 0, "source": "category"}

    for key, paper_ids in category_papers.items():
        existing = suggestions.get(key)
        if existing is None:
            continue
        existing["paper_count"] = max(int(existing.get("paper_count") or 0), len(paper_ids))
        sources = str(existing.get("source") or "").split("+")
        if "category" not in sources:
            sources.append("category")
            existing["source"] = "+".join(source for source in sources if source)

    ordered = sorted(
        suggestions.values(),
        key=lambda item: (
            0 if str(item.get("source") or "").startswith("saved") else 1,
            -int(item.get("paper_count") or 0),
            str(item.get("tag") or "").casefold(),
        ),
    )
    return scrub_public_payload(ordered[:limit])


@router.get("/quality-report")
def get_local_quality_report() -> dict[str, Any]:
    with db_session() as connection:
        paper_rows = connection.execute(
            """
            SELECT
                paper_id, title, authors, year, journal, doi, file_path,
                file_hash, page_count, created_at, updated_at
            FROM papers
            ORDER BY updated_at DESC
            """
        ).fetchall()
        draft_rows = connection.execute(
            """
            SELECT paper_id, COUNT(*) AS draft_count
            FROM paper_ai_drafts
            WHERE status != 'rejected'
            GROUP BY paper_id
            """
        ).fetchall()
        annotation_rows = connection.execute(
            """
            SELECT paper_id, COUNT(*) AS annotation_count
            FROM paper_annotations
            GROUP BY paper_id
            """
        ).fetchall()
        duplicate_hash_rows = connection.execute(
            """
            SELECT file_hash, COUNT(*) AS duplicate_count
            FROM papers
            GROUP BY file_hash
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        tag_rows = connection.execute(
            """
            SELECT paper_id, COUNT(*) AS tag_count
            FROM paper_tags
            GROUP BY paper_id
            """
        ).fetchall()

    draft_counts = {row["paper_id"]: int(row["draft_count"] or 0) for row in draft_rows}
    annotation_counts = {
        row["paper_id"]: int(row["annotation_count"] or 0) for row in annotation_rows
    }
    duplicate_hashes = {row["file_hash"] for row in duplicate_hash_rows}
    tag_counts = {row["paper_id"]: int(row["tag_count"] or 0) for row in tag_rows}
    papers = [_paper_public(row) for row in paper_rows]
    by_id = {paper["paper_id"]: paper for paper in papers}
    try:
        from .library_organizer import organize_library

        duplicate_files = organize_library(
            dry_run=True,
            include_duplicates=False,
            apply_moves=False,
        ).get("duplicate_files", [])
    except Exception:
        duplicate_files = []

    issues: list[dict[str, Any]] = []
    summary = {
        "missing_doi": 0,
        "missing_authors": 0,
        "missing_year": 0,
        "future_year": 0,
        "duplicate_files": 0,
        "uncategorized": 0,
        "missing_tags": 0,
        "no_summary": 0,
    }

    for row, paper in zip(paper_rows, papers):
        if not paper.get("doi"):
            summary["missing_doi"] += 1
            issues.append(_quality_issue("missing_doi", "缺 DOI", paper))
        if not paper.get("authors"):
            summary["missing_authors"] += 1
            issues.append(_quality_issue("missing_authors", "缺作者", paper))
        if paper.get("year") is None:
            summary["missing_year"] += 1
            issues.append(_quality_issue("missing_year", "缺年份", paper))
        elif int(paper["year"]) > 2026:
            summary["future_year"] += 1
            issues.append(_quality_issue("future_year", "年份异常", paper, str(paper["year"])))
        if row["file_hash"] in duplicate_hashes:
            summary["duplicate_files"] += 1
            issues.append(_quality_issue("duplicate_files", "重复文件", paper))
        if not paper.get("category_path"):
            summary["uncategorized"] += 1
            issues.append(_quality_issue("uncategorized", "未分类", paper))
        if not tag_counts.get(paper["paper_id"]):
            summary["missing_tags"] += 1
            issues.append(_quality_issue("missing_tags", "未打标签", paper))
        if not draft_counts.get(paper["paper_id"]) and not annotation_counts.get(paper["paper_id"]):
            summary["no_summary"] += 1
            issues.append(_quality_issue("no_summary", "未生成摘要", paper))

    for duplicate in duplicate_files:
        paper = by_id.get(duplicate.get("matches_paper_id"))
        if paper is None:
            continue
        summary["duplicate_files"] += 1
        issues.append(
            _quality_issue(
                "duplicate_files",
                "重复文件",
                paper,
                duplicate.get("source", {}).get("path", ""),
            )
        )

    return scrub_public_payload(
        {
            "paper_count": len(papers),
            "summary": summary,
            "issues": issues[:400],
            "issue_count": len(issues),
        }
    )


@router.get("/papers")
def list_or_search_papers(
    query: str = "",
    limit: int = LOCAL_LIBRARY_LIMIT,
    category_path: str = ALL_CATEGORIES,
    tag: str = "",
) -> list[dict[str, Any]]:
    query = query.strip()
    limit = max(1, min(int(limit or LOCAL_LIBRARY_LIMIT), LOCAL_LIBRARY_LIMIT_MAX))
    category_filter = (
        None if category_path == ALL_CATEGORIES else paper_files.normalize_category_path(category_path)
    )
    tag_filter = _clean_tag_filter(tag)

    if query:
        records = search_papers(query=query, limit=min(limit, 10))
        paper_ids = [record["paper_id"] for record in records]
        with db_session() as connection:
            placeholders = ",".join("?" for _ in paper_ids)
            detail_rows = (
                connection.execute(
                    f"""
                    SELECT
                        paper_id, title, authors, year, journal, doi, file_path,
                        page_count, created_at, updated_at
                    FROM papers
                    WHERE paper_id IN ({placeholders})
                    """,
                    tuple(paper_ids),
                ).fetchall()
                if paper_ids
                else []
            )
            tag_map = _tags_by_paper_ids(connection, paper_ids)
            reading_map = _reading_states_by_paper_ids(connection, paper_ids)
        details = {
            row["paper_id"]: _paper_public(
                row,
                tags=tag_map.get(row["paper_id"], []),
                reading_state=reading_map.get(row["paper_id"]),
            )
            for row in detail_rows
        }
        enriched = []
        for record in records:
            detail = details.get(record["paper_id"], {})
            merged = {**detail, **record}
            if _paper_matches_filters(merged, category_filter, tag_filter):
                enriched.append(merged)
        return scrub_public_payload(enriched)

    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT
                paper_id, title, authors, year, journal, doi, file_path,
                page_count, created_at, updated_at
            FROM papers
            ORDER BY updated_at DESC
            """,
        ).fetchall()
        paper_ids = [row["paper_id"] for row in rows]
        tag_map = _tags_by_paper_ids(connection, paper_ids)
        reading_map = _reading_states_by_paper_ids(connection, paper_ids)
    papers = [
        _paper_public(
            row,
            tags=tag_map.get(row["paper_id"], []),
            reading_state=reading_map.get(row["paper_id"]),
        )
        for row in rows
    ]
    papers = [
        paper
        for paper in papers
        if _paper_matches_filters(paper, category_filter, tag_filter)
    ]
    return scrub_public_payload(papers[:limit])


@router.get("/papers/{paper_id}")
def get_local_paper(paper_id: str) -> dict[str, Any]:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                paper_id, title, authors, year, journal, doi, file_path,
                page_count, created_at, updated_at
            FROM papers
            WHERE paper_id = ?
            """,
            (paper_id,),
        ).fetchone()
        tags = _tags_by_paper_ids(connection, [paper_id]).get(paper_id, [])
        reading_state = _reading_states_by_paper_ids(connection, [paper_id]).get(paper_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="paper not found.")
    return scrub_public_payload(_paper_public(row, tags=tags, reading_state=reading_state))


@router.get("/papers/{paper_id}/tags")
def get_local_paper_tags(paper_id: str) -> dict[str, Any]:
    with db_session() as connection:
        if not _paper_exists(connection, paper_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="paper not found.")
        tags = _tags_by_paper_ids(connection, [paper_id]).get(paper_id, [])
    return scrub_public_payload({"paper_id": paper_id, "tags": tags})


@router.put("/papers/{paper_id}/tags")
def update_local_paper_tags(paper_id: str, payload: PaperTagsRequest) -> dict[str, Any]:
    tags = _clean_tags(payload.tags)
    now = utcnow()
    with db_session() as connection:
        if not _paper_exists(connection, paper_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="paper not found.")
        connection.execute("DELETE FROM paper_tags WHERE paper_id = ?", (paper_id,))
        connection.executemany(
            """
            INSERT OR IGNORE INTO paper_tags (paper_id, tag, created_at)
            VALUES (?, ?, ?)
            """,
            [(paper_id, tag_item, now) for tag_item in tags],
        )
    return scrub_public_payload({"paper_id": paper_id, "tags": tags})


@router.put("/papers/{paper_id}/metadata")
def update_local_paper_metadata(
    paper_id: str,
    payload: PaperMetadataRequest,
) -> dict[str, Any]:
    clean = _clean_paper_metadata(payload)
    now = utcnow()
    with db_session() as connection:
        if not _paper_exists(connection, paper_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="paper not found.")
        connection.execute(
            """
            UPDATE papers
            SET title = ?, authors = ?, year = ?, journal = ?, doi = ?, updated_at = ?
            WHERE paper_id = ?
            """,
            (
                clean["title"],
                clean["authors"],
                clean["year"],
                clean["journal"],
                clean["doi"],
                now,
                paper_id,
            ),
        )
        row = connection.execute(
            """
            SELECT
                paper_id, title, authors, year, journal, doi, file_path,
                page_count, created_at, updated_at
            FROM papers
            WHERE paper_id = ?
            """,
            (paper_id,),
        ).fetchone()
        tags = _tags_by_paper_ids(connection, [paper_id]).get(paper_id, [])
        reading_state = _reading_states_by_paper_ids(connection, [paper_id]).get(paper_id)
    return scrub_public_payload(_paper_public(row, tags=tags, reading_state=reading_state))


@router.get("/papers/{paper_id}/reading-state")
def get_local_paper_reading_state(paper_id: str) -> dict[str, Any]:
    with db_session() as connection:
        if not _paper_exists(connection, paper_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="paper not found.")
        reading_state = _reading_states_by_paper_ids(connection, [paper_id]).get(
            paper_id,
            _default_reading_state(),
        )
    return scrub_public_payload({"paper_id": paper_id, **reading_state})


@router.put("/papers/{paper_id}/reading-state")
def update_local_paper_reading_state(
    paper_id: str,
    payload: PaperReadingStateRequest,
) -> dict[str, Any]:
    reading_state = _clean_reading_state(payload)
    now = utcnow()
    with db_session() as connection:
        if not _paper_exists(connection, paper_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="paper not found.")
        connection.execute(
            """
            INSERT INTO paper_reading_states (
                paper_id, read_status, is_favorite, is_later, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(paper_id) DO UPDATE SET
                read_status = excluded.read_status,
                is_favorite = excluded.is_favorite,
                is_later = excluded.is_later,
                updated_at = excluded.updated_at
            """,
            (
                paper_id,
                reading_state["read_status"],
                1 if reading_state["is_favorite"] else 0,
                1 if reading_state["is_later"] else 0,
                now,
                now,
            ),
        )
    return scrub_public_payload({"paper_id": paper_id, **reading_state})


@router.post("/papers/{paper_id}/move")
def move_local_paper(paper_id: str, payload: PaperMoveRequest) -> dict[str, Any]:
    return scrub_public_payload(
        paper_files.move_paper_file(
            paper_id=paper_id,
            category_path=payload.category_path,
            create_missing_category=payload.create_missing_category,
            overwrite_existing=payload.overwrite_existing,
        )
    )


@router.get("/papers/{paper_id}/overview")
def get_local_paper_overview(paper_id: str) -> dict[str, Any]:
    with db_session() as connection:
        paper_row = connection.execute(
            """
            SELECT
                paper_id, title, authors, year, journal, doi, file_path,
                page_count, created_at, updated_at
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


@router.get("/papers/{paper_id}/page-search")
def search_local_paper_pages(
    paper_id: str,
    query: str,
    limit: int = 20,
) -> dict[str, Any]:
    query = _compact_text(query)
    if not query:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="query is required.")
    limit = max(1, min(int(limit or 20), 50))
    with db_session() as connection:
        if not _paper_exists(connection, paper_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="paper not found.")
        rows = connection.execute(
            """
            SELECT page_number, text
            FROM paper_pages
            WHERE paper_id = ?
            ORDER BY page_number
            """,
            (paper_id,),
        ).fetchall()

    results: list[dict[str, Any]] = []
    query_key = query.casefold()
    for row in rows:
        text = row["text"] or ""
        if query_key not in text.casefold():
            continue
        results.append(
            {
                "page_number": row["page_number"],
                "snippet": _page_search_snippet(text, query),
            }
        )
        if len(results) >= limit:
            break
    return scrub_public_payload({"paper_id": paper_id, "query": query, "results": results})


@router.post("/papers/{paper_id}/drafts/generate")
def generate_local_paper_draft(paper_id: str) -> dict[str, Any]:
    now = utcnow()
    draft_id = f"d_{uuid.uuid4().hex[:20]}"
    with db_session() as connection:
        paper_row = connection.execute(
            """
            SELECT
                paper_id, title, authors, year, journal, doi, file_path,
                page_count, created_at, updated_at
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
        paper = _paper_public(paper_row)
        annotation = _draft_from_indexed_text(paper, page_rows)
        connection.execute(
            """
            INSERT INTO paper_ai_drafts (
                draft_id, paper_id, annotation_json, status, created_at, updated_at
            )
            VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (draft_id, paper_id, json.dumps(annotation, ensure_ascii=False), now, now),
        )

    return scrub_public_payload(
        {
            "draft_id": draft_id,
            "paper_id": paper_id,
            "annotation_json": annotation,
            "status": "pending",
            "title": paper["title"],
            "authors": paper["authors"],
            "year": paper["year"],
            "journal": paper["journal"],
            "doi": paper["doi"],
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

        recommended_tags = _clean_recommended_tags(annotation.get("recommended_tags") or [])
        connection.executemany(
            """
            INSERT OR IGNORE INTO paper_tags (paper_id, tag, created_at)
            VALUES (?, ?, ?)
            """,
            [(paper_id, tag, now) for tag in recommended_tags],
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
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"", "none", "null", "unknown"}:
            return None
        if normalized in {"false", "0", "no", "n", "off"}:
            return 0
        if normalized in {"true", "1", "yes", "y", "on"}:
            return 1
    return 1 if bool(value) else 0

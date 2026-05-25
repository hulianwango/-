from __future__ import annotations

import hashlib
import re
from typing import Any

from .config import settings
from .database import db_session, utcnow
from .tencent_translate import (
    TranslationError,
    translate_text,
    translation_configured,
    translation_unavailable_reason,
)


OVERVIEW_TRANSLATION_PROVIDER = "tencent"


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _overview_source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def strict_translation_missing_reason(status_payload: dict[str, Any]) -> str:
    if not status_payload.get("source_count"):
        return "没有可用于严格翻译的英文来源文本。"
    if not status_payload.get("configured"):
        return "腾讯云翻译未配置，暂时没有生成中文翻译。"
    if status_payload.get("error"):
        return f"腾讯云翻译失败：{status_payload['error']}"
    return "腾讯云严格中文翻译尚未生成。"


def strict_overview_translations(
    paper_id: str,
    sources: dict[str, str],
) -> tuple[dict[str, str], dict[str, Any]]:
    entries: dict[str, dict[str, str]] = {}
    translations: dict[str, str] = {}
    for source_key, source_text in sources.items():
        text = _compact_text(source_text)
        if not text:
            translations[source_key] = ""
            continue
        entries[source_key] = {
            "text": text,
            "source_hash": _overview_source_hash(text),
        }

    status_payload: dict[str, Any] = {
        "provider": OVERVIEW_TRANSLATION_PROVIDER,
        "target_language": settings.translation_target_language,
        "configured": translation_configured(),
        "source_count": len(entries),
        "cached_count": 0,
        "translated_count": 0,
        "missing_count": 0,
        "error": "",
    }

    if entries:
        with db_session() as connection:
            for source_key, entry in entries.items():
                row = connection.execute(
                    """
                    SELECT translated_text
                    FROM paper_overview_translations
                    WHERE paper_id = ?
                      AND source_key = ?
                      AND source_hash = ?
                      AND target_language = ?
                    LIMIT 1
                    """,
                    (
                        paper_id,
                        source_key,
                        entry["source_hash"],
                        settings.translation_target_language,
                    ),
                ).fetchone()
                if row is not None:
                    translations[source_key] = row["translated_text"]
                    status_payload["cached_count"] += 1

    missing_keys = [
        source_key for source_key in entries if not translations.get(source_key)
    ]
    translated_rows: list[tuple[str, str, str]] = []

    if missing_keys and not translation_configured():
        status_payload["error"] = translation_unavailable_reason()
    elif missing_keys:
        for source_key in missing_keys:
            entry = entries[source_key]
            try:
                translated_text = translate_text(entry["text"])
            except TranslationError as error:
                status_payload["error"] = str(error)
                translations[source_key] = ""
                continue
            translations[source_key] = translated_text
            translated_rows.append((source_key, entry["source_hash"], translated_text))
            status_payload["translated_count"] += 1

    if translated_rows:
        now = utcnow()
        with db_session() as connection:
            for source_key, source_hash, translated_text in translated_rows:
                connection.execute(
                    """
                    INSERT INTO paper_overview_translations (
                        paper_id, source_key, source_hash, target_language,
                        provider, translated_text, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(paper_id, source_key, source_hash, target_language)
                    DO UPDATE SET
                        provider = excluded.provider,
                        translated_text = excluded.translated_text,
                        updated_at = excluded.updated_at
                    """,
                    (
                        paper_id,
                        source_key,
                        source_hash,
                        settings.translation_target_language,
                        OVERVIEW_TRANSLATION_PROVIDER,
                        translated_text,
                        now,
                        now,
                    ),
                )

    for source_key in sources:
        translations.setdefault(source_key, "")
    status_payload["missing_count"] = sum(
        1 for source_key in entries if not translations.get(source_key)
    )
    return translations, status_payload

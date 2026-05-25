from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import settings


TENCENT_TMT_HOST = "tmt.tencentcloudapi.com"
TENCENT_TMT_ENDPOINT = f"https://{TENCENT_TMT_HOST}"
TENCENT_TMT_SERVICE = "tmt"
TENCENT_TMT_VERSION = "2018-03-21"
TRANSLATION_CHUNK_LIMIT = 4500


class TranslationError(RuntimeError):
    pass


def translation_configured() -> bool:
    return (
        settings.translation_enabled
        and settings.translation_provider.casefold() == "tencent"
        and bool(settings.tencent_translate_secret_id)
        and bool(settings.tencent_translate_secret_key)
    )


def translation_unavailable_reason() -> str:
    if not settings.translation_enabled:
        return "本地配置已关闭翻译。"
    if settings.translation_provider.casefold() != "tencent":
        return "当前只支持腾讯云翻译。"
    if not settings.tencent_translate_secret_id or not settings.tencent_translate_secret_key:
        return "腾讯云翻译密钥未配置。"
    return ""


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _split_text(text: str, limit: int = TRANSLATION_CHUNK_LIMIT) -> list[str]:
    text = str(text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for paragraph in text.splitlines():
        part = paragraph.strip()
        if not part:
            continue
        if len(part) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(part[index : index + limit] for index in range(0, len(part), limit))
            continue
        candidate = f"{current}\n{part}".strip() if current else part
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = part
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _tencent_request(action: str, payload: dict[str, object], timeout_seconds: float = 12.0) -> dict:
    timestamp = int(time.time())
    date = datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")
    request_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    canonical_headers = (
        "content-type:application/json; charset=utf-8\n"
        f"host:{TENCENT_TMT_HOST}\n"
        f"x-tc-action:{action.casefold()}\n"
    )
    signed_headers = "content-type;host;x-tc-action"
    canonical_request = "\n".join(
        [
            "POST",
            "/",
            "",
            canonical_headers,
            signed_headers,
            _sha256_hex(request_payload),
        ]
    )
    credential_scope = f"{date}/{TENCENT_TMT_SERVICE}/tc3_request"
    string_to_sign = "\n".join(
        [
            "TC3-HMAC-SHA256",
            str(timestamp),
            credential_scope,
            _sha256_hex(canonical_request),
        ]
    )
    secret_date = _hmac_sha256(
        f"TC3{settings.tencent_translate_secret_key}".encode("utf-8"), date
    )
    secret_service = _hmac_sha256(secret_date, TENCENT_TMT_SERVICE)
    secret_signing = _hmac_sha256(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    authorization = (
        "TC3-HMAC-SHA256 "
        f"Credential={settings.tencent_translate_secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    request = Request(
        TENCENT_TMT_ENDPOINT,
        data=request_payload.encode("utf-8"),
        headers={
            "Authorization": authorization,
            "Content-Type": "application/json; charset=utf-8",
            "Host": TENCENT_TMT_HOST,
            "X-TC-Action": action,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": TENCENT_TMT_VERSION,
            "X-TC-Region": settings.tencent_translate_region,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise TranslationError(f"Tencent translation HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise TranslationError(f"Tencent translation network error: {error.reason}") from error

    loaded = json.loads(raw)
    response_payload = loaded.get("Response", {})
    if response_payload.get("Error"):
        api_error = response_payload["Error"]
        code = api_error.get("Code", "Unknown")
        message = api_error.get("Message", "Tencent translation failed.")
        raise TranslationError(f"{code}: {message}")
    return response_payload


def translate_text(text: str) -> str:
    if not translation_configured():
        raise TranslationError(translation_unavailable_reason())

    chunks = _split_text(text)
    if not chunks:
        return ""

    translated_chunks: list[str] = []
    for chunk in chunks:
        response = _tencent_request(
            "TextTranslate",
            {
                "SourceText": chunk,
                "Source": settings.translation_source_language,
                "Target": settings.translation_target_language,
                "ProjectId": settings.tencent_translate_project_id,
            },
        )
        target_text = str(response.get("TargetText") or "").strip()
        if not target_text:
            raise TranslationError("Tencent translation returned an empty TargetText.")
        translated_chunks.append(target_text)

    return "\n\n".join(translated_chunks).strip()

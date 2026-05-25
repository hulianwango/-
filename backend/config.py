from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Config file {path.name} must contain a mapping.")
    return loaded


def _deep_get(data: dict[str, Any], dotted_key: str, default: Any) -> Any:
    current: Any = data
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _first_env(merged_env: dict[str, str], *keys: str, default: str = "") -> str:
    for key in keys:
        value = merged_env.get(key)
        if value:
            return value
    return default


def _first_config(config: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = _deep_get(config, key, None)
        if value not in (None, ""):
            return value
    return default


def _resolve_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


@dataclass(frozen=True)
class Settings:
    app_host: str
    app_port: int
    papers_dir: Path
    database_path: Path
    logs_dir: Path
    mcp_bearer_token: str
    mcp_require_auth: bool
    rate_limit_per_minute: int
    max_search_limit: int
    max_chunks_per_request: int
    max_response_chars: int
    oauth_enabled: bool
    oauth_public_base_url: str
    oauth_username: str
    oauth_password_hash: str
    oauth_token_expires_seconds: int
    oauth_code_expires_seconds: int
    chunk_size: int
    chunk_overlap: int
    translation_provider: str
    translation_enabled: bool
    translation_source_language: str
    translation_target_language: str
    tencent_translate_secret_id: str
    tencent_translate_secret_key: str
    tencent_translate_region: str
    tencent_translate_project_id: int


def load_settings() -> Settings:
    dotenv = _read_dotenv(PROJECT_ROOT / ".env")
    merged_env = {**dotenv, **os.environ}

    config_path = PROJECT_ROOT / "config.local.yaml"
    if not config_path.exists():
        config_path = PROJECT_ROOT / "config.example.yaml"
    config = _load_yaml(config_path)

    papers_dir = merged_env.get("LIT_PAPERS_DIR") or _deep_get(
        config, "paths.papers_dir", "papers"
    )
    database = merged_env.get("LIT_DATABASE_PATH") or _deep_get(
        config, "paths.database", "data/library.db"
    )
    logs_dir = merged_env.get("LIT_LOGS_DIR") or _deep_get(
        config, "paths.logs_dir", "logs"
    )
    translation_provider = str(
        merged_env.get("TRANSLATION_PROVIDER")
        or _deep_get(config, "translation.provider", "tencent")
    ).strip()
    tencent_translate_secret_id = str(
        _first_env(
            merged_env,
            "TENCENTCLOUD_SECRET_ID",
            "TENCENTCLOUD_SECRETID",
            "TENCENT_SECRET_ID",
            "TENCENT_SECRETID",
            "QCLOUD_SECRET_ID",
            default=str(
                _first_config(
                    config,
                    "translation.tencent.secret_id",
                    "translation.tencent.secretId",
                    default="",
                )
            ),
        )
    ).strip()
    tencent_translate_secret_key = str(
        _first_env(
            merged_env,
            "TENCENTCLOUD_SECRET_KEY",
            "TENCENTCLOUD_SECRETKEY",
            "TENCENT_SECRET_KEY",
            "TENCENT_SECRETKEY",
            "QCLOUD_SECRET_KEY",
            default=str(
                _first_config(
                    config,
                    "translation.tencent.secret_key",
                    "translation.tencent.secretKey",
                    default="",
                )
            ),
        )
    ).strip()
    translation_enabled = _as_bool(
        merged_env.get("TRANSLATION_ENABLED"),
        _as_bool(_deep_get(config, "translation.enabled", True), True),
    )

    return Settings(
        app_host=str(_deep_get(config, "app.host", "127.0.0.1")),
        app_port=int(_deep_get(config, "app.port", 8000)),
        papers_dir=_resolve_path(str(papers_dir)),
        database_path=_resolve_path(str(database)),
        logs_dir=_resolve_path(str(logs_dir)),
        mcp_bearer_token=str(
            merged_env.get("MCP_BEARER_TOKEN")
            or _deep_get(config, "mcp.bearer_token", "")
        ),
        mcp_require_auth=_as_bool(
            merged_env.get("MCP_REQUIRE_AUTH"),
            _as_bool(_deep_get(config, "mcp.require_auth", True), True),
        ),
        rate_limit_per_minute=int(_deep_get(config, "mcp.rate_limit_per_minute", 60)),
        max_search_limit=int(_deep_get(config, "mcp.max_search_limit", 10)),
        max_chunks_per_request=int(_deep_get(config, "mcp.max_chunks_per_request", 5)),
        max_response_chars=int(_deep_get(config, "mcp.max_response_chars", 6000)),
        oauth_enabled=_as_bool(
            merged_env.get("OAUTH_ENABLED"),
            _as_bool(_deep_get(config, "oauth.enabled", True), True),
        ),
        oauth_public_base_url=str(
            merged_env.get("OAUTH_PUBLIC_BASE_URL")
            or _deep_get(config, "oauth.public_base_url", "")
        ).rstrip("/"),
        oauth_username=str(
            merged_env.get("OAUTH_USERNAME")
            or _deep_get(config, "oauth.username", "")
        ),
        oauth_password_hash=str(
            merged_env.get("OAUTH_PASSWORD_HASH")
            or _deep_get(config, "oauth.password_hash", "")
        ),
        oauth_token_expires_seconds=int(
            merged_env.get("OAUTH_TOKEN_EXPIRES_SECONDS")
            or _deep_get(config, "oauth.token_expires_seconds", 43200)
        ),
        oauth_code_expires_seconds=int(
            merged_env.get("OAUTH_CODE_EXPIRES_SECONDS")
            or _deep_get(config, "oauth.code_expires_seconds", 600)
        ),
        chunk_size=int(_deep_get(config, "index.chunk_size", 1800)),
        chunk_overlap=int(_deep_get(config, "index.chunk_overlap", 250)),
        translation_provider=translation_provider,
        translation_enabled=translation_enabled,
        translation_source_language=str(
            merged_env.get("TRANSLATION_SOURCE_LANGUAGE")
            or _deep_get(config, "translation.source_language", "en")
        ).strip()
        or "en",
        translation_target_language=str(
            merged_env.get("TRANSLATION_TARGET_LANGUAGE")
            or _deep_get(config, "translation.target_language", "zh")
        ).strip()
        or "zh",
        tencent_translate_secret_id=tencent_translate_secret_id,
        tencent_translate_secret_key=tencent_translate_secret_key,
        tencent_translate_region=str(
            _first_env(
                merged_env,
                "TENCENTCLOUD_REGION",
                "TENCENT_REGION",
                "QCLOUD_REGION",
                default=str(_deep_get(config, "translation.tencent.region", "ap-guangzhou")),
            )
        ).strip()
        or "ap-guangzhou",
        tencent_translate_project_id=int(
            merged_env.get("TENCENTCLOUD_TMT_PROJECT_ID")
            or _deep_get(config, "translation.tencent.project_id", 0)
        ),
    )


settings = load_settings()

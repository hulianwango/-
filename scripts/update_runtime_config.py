from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - only hit before dependencies install.
    raise SystemExit(
        "PyYAML is not installed. Run: python -m pip install -r requirements.txt"
    ) from exc


def _load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Config file was not found: {path}")

    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise SystemExit(f"Config file must contain a YAML mapping: {path}")
    return loaded


def _section(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.setdefault(name, {})
    if not isinstance(value, dict):
        raise SystemExit(f"Config section must be a mapping: {name}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update runtime tunnel URL and MCP bearer token in config.local.yaml."
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--public-base-url", required=True)
    parser.add_argument("--bearer-token", default="")
    parser.add_argument("--generate-token", action="store_true")
    parser.add_argument("--keep-token", action="store_true")
    parser.add_argument("--app-port", type=int, default=0)
    parser.add_argument("--oauth-username", default="")
    parser.add_argument("--oauth-password", default="")
    parser.add_argument("--output-json", action="store_true")
    args = parser.parse_args()

    if args.generate_token and args.keep_token:
        raise SystemExit("--generate-token and --keep-token cannot be used together.")

    config_path = args.config.resolve()
    config = _load_config(config_path)
    app = _section(config, "app")
    mcp = _section(config, "mcp")
    oauth = _section(config, "oauth")

    public_base_url = args.public_base_url.rstrip("/")
    if not public_base_url.startswith("https://"):
        raise SystemExit("public base URL must start with https://")
    if args.app_port:
        if args.app_port < 1 or args.app_port > 65535:
            raise SystemExit("app port must be between 1 and 65535")
        app["port"] = args.app_port

    if args.generate_token:
        bearer_token = secrets.token_hex(32)
        mcp["bearer_token"] = bearer_token
    elif args.bearer_token:
        bearer_token = args.bearer_token
        mcp["bearer_token"] = bearer_token
    else:
        bearer_token = str(mcp.get("bearer_token") or "")

    oauth["public_base_url"] = public_base_url
    if args.oauth_username:
        oauth["username"] = args.oauth_username
    if args.oauth_password:
        try:
            import bcrypt
        except ImportError as exc:  # pragma: no cover - only hit before dependencies install.
            raise SystemExit(
                "bcrypt is not installed. Run: python -m pip install -r requirements.txt"
            ) from exc
        oauth["password_hash"] = bcrypt.hashpw(
            args.oauth_password.encode("utf-8"), bcrypt.gensalt()
        ).decode("utf-8")

    config_text = yaml.safe_dump(
        config,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    config_path.write_text(config_text, encoding="utf-8")

    result = {
        "config": str(config_path),
        "public_base_url": public_base_url,
        "app_port": int(app.get("port") or 0),
        "bearer_token": bearer_token,
        "oauth_username": str(oauth.get("username") or ""),
    }
    if args.output_json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"Updated {config_path}")
        print(f"public_base_url={public_base_url}")
        print(f"bearer_token={bearer_token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Small operator CLI.

    python -m app.cli keygen           # mint a caller API key
    python -m app.cli keygen --webhook # mint a webhook signing secret
    python -m app.cli check            # validate the current environment
"""

from __future__ import annotations

import argparse
import sys

from .config import Settings, generate_api_key


def _check() -> int:
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001
        print(f"config invalid: {exc}", file=sys.stderr)
        return 1

    from .providers import configured_providers

    providers = [s.id for s in configured_providers(settings)]
    print(f"environment      : {settings.environment}")
    print(f"auth             : {'DISABLED' if settings.auth_disabled else f'{len(settings.api_key_set)} key(s)'}")
    print(f"cors origins     : {', '.join(settings.cors_origin_list) or '(none)'}")
    print(f"providers ready  : {', '.join(providers) or '(none)'}")
    print(f"tavily search    : {'yes' if settings.tavily_api_key else 'no (provider-native fallback)'}")
    print(f"webhook signing  : {'configured' if settings.webhook_secret else 'NOT configured'}")

    problems = []
    if not providers:
        problems.append("no provider credentials -- /v1/research will return 503")
    if not settings.auth_disabled and not settings.api_key_set:
        problems.append("no API_KEYS -- every authenticated route will return 503")

    for problem in problems:
        print(f"  ! {problem}", file=sys.stderr)
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    keygen = sub.add_parser("keygen", help="Generate a secret")
    keygen.add_argument(
        "--webhook", action="store_true", help="Generate a webhook signing secret instead"
    )
    sub.add_parser("check", help="Validate the current environment")

    args = parser.parse_args()
    if args.command == "keygen":
        print(generate_api_key("whsec" if args.webhook else "drk"))
        return 0
    return _check()


if __name__ == "__main__":
    raise SystemExit(main())

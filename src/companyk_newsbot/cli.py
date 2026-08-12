"""Commands intentionally limited to configuration validation in Step 1."""

from __future__ import annotations

import argparse
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, ConfigLoadError, load_keyword_map


def main() -> int:
    parser = argparse.ArgumentParser(prog="companyk-newsbot")
    subcommands = parser.add_subparsers(dest="command", required=True)
    validate = subcommands.add_parser("validate-config", help="Validate a keyword map")
    validate.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    args = parser.parse_args()

    try:
        config = load_keyword_map(args.config)
    except ConfigLoadError as exc:
        parser.error(str(exc))
    print(
        f"valid: {args.config} (schema={config.schema_version}, "
        f"companies={len(config.company_rules)})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pensae.api.app import create_app
from pensae.config.settings import BootstrapSettings, Environment
from pensae.infrastructure.health.service import unavailable_health_service


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the canonical Pensae Signal OpenAPI schema"
    )
    parser.add_argument("--output", type=Path, default=Path("openapi.json"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = BootstrapSettings.model_validate({"environment": Environment.TEST})
    schema = create_app(settings=settings, health_service=unavailable_health_service()).openapi()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(schema, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

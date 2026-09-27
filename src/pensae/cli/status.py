from __future__ import annotations

import argparse
import asyncio
import json

from pensae.config.settings import BootstrapSettings
from pensae.infrastructure.health.live import live_health_service


async def _status(*, as_json: bool) -> int:
    settings = BootstrapSettings()
    result = await live_health_service(settings).preflight()
    if as_json:
        print(json.dumps(result.model_dump(mode="json"), indent=2))
    else:
        print(f"Pensae Signal capability preflight: {'READY' if result.ready else 'DEGRADED'}")
        for check in result.checks:
            print(f"- {check.dependency.value}: {check.state.value} — {check.summary}")
            if check.action:
                print(f"  action: {check.action}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Show safe local Pensae Signal dependency health")
    parser.add_argument("--json", action="store_true", help="emit structured JSON")
    args = parser.parse_args()
    return asyncio.run(_status(as_json=args.json))


if __name__ == "__main__":
    raise SystemExit(main())

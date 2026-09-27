"""Bounded, volume-preserving teardown, including pre-secret-migration installs."""

import asyncio

from scripts.linux.launcher import run_fixed

if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_fixed(("docker", "compose", "down", "--remove-orphans"))))

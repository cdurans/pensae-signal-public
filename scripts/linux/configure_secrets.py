"""Create private installation configuration without modifying existing state."""

from __future__ import annotations

import os
import secrets
from pathlib import Path


def initialize(root: Path) -> bool:
    """Create one new installation file exclusively; preserve any existing path."""
    destination = root / ".env"
    template = (root / ".env.example").read_text(encoding="utf-8")
    password = secrets.token_hex(32)
    replacements = {
        "POSTGRES_PASSWORD": password,
        "PENSAE_POSTGRES_DSN": f"postgresql+psycopg://pensae:{password}@127.0.0.1:5432/pensae",
        "SEARXNG_SECRET": secrets.token_hex(32),
    }
    seen: set[str] = set()
    lines: list[str] = []
    for line in template.splitlines():
        key = line.partition("=")[0]
        if key in replacements:
            if key in seen:
                raise ValueError("installation template contains a duplicate secret field")
            seen.add(key)
            line = f"{key}={replacements[key]}"
        lines.append(line)
    if seen != set(replacements):
        raise ValueError("installation template is missing required secret fields")
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return True


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    try:
        created = initialize(root)
    except (OSError, ValueError):
        print("Could not create private installation configuration; no credentials were displayed.")
        return 2
    if created:
        print(
            "Created private .env with unique service secrets. Set your model paths before start."
        )
    else:
        print("Existing .env preserved. See the installation guide for service-secret migration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# Third-party dependency and image notices

Inventory date: 2026-07-23. Versions below are resolved by the committed locks. This is a release
control, not legal advice and not a substitute for the complete upstream license text. Pensae Signal does
not vendor dependency source, container layers, llama.cpp binaries, or model artifacts. Container
images and llama.cpp are pulled or acquired separately and are not vendored or repackaged.

## Python direct dependencies

| Package | Locked version | Upstream license expression recorded in installed package metadata |
|---|---:|---|
| `aiohttp` | `3.14.2` | Apache-2.0 AND MIT |
| `alembic` | `1.18.5` | MIT |
| `fastapi` | `0.139.2` | MIT |
| `httpx` | `0.28.1` | BSD-3-Clause |
| `langgraph` | `1.2.9` | MIT |
| `pgvector` | `0.5.0` | MIT |
| `psycopg` | `3.3.4` | LGPL-3.0-only |
| `pydantic` | `2.13.4` | MIT |
| `pydantic-settings` | `2.14.2` | MIT |
| `pyyaml` | `6.0.3` | MIT |
| `redis` | `6.4.0` | MIT |
| `sqlalchemy` | `2.0.51` | MIT |
| `trafilatura` | `2.1.0` | Apache-2.0 |
| `uvicorn` | `0.51.0` | BSD-3-Clause |

Development-only direct dependencies:

| Package | Locked version | Upstream license expression recorded in installed package metadata |
|---|---:|---|
| `anyio` | `4.14.2` | MIT |
| `pyright` | `1.1.411` | MIT |
| `pytest` | `9.1.1` | MIT |
| `pytest-cov` | `7.1.0` | MIT |
| `ruff` | `0.15.22` | MIT |

All Python transitive versions and artifact hashes are recorded in `uv.lock`. Review the complete
transitive license set before distributing a built Python environment rather than this source tree.

## Frontend direct dependencies

| Package | Locked version | Upstream license expression recorded in installed package metadata |
|---|---:|---|
| `@tanstack/react-query` | `5.101.3` | MIT |
| `react` | `19.2.7` | MIT |
| `react-dom` | `19.2.7` | MIT |
| `react-router` | `7.18.1` | MIT |

Development-only direct dependencies:

| Package | Locked version | Upstream license expression recorded in installed package metadata |
|---|---:|---|
| `@biomejs/biome` | `2.5.4` | MIT OR Apache-2.0 |
| `@hey-api/openapi-ts` | `0.86.12` | MIT |
| `@playwright/test` | `1.61.1` | Apache-2.0 |
| `@testing-library/jest-dom` | `6.10.0` | MIT |
| `@testing-library/react` | `16.3.2` | MIT |
| `@testing-library/user-event` | `14.6.1` | MIT |
| `@types/react` | `19.2.17` | MIT |
| `@types/react-dom` | `19.2.3` | MIT |
| `@vitejs/plugin-react` | `5.2.0` | MIT |
| `jsdom` | `27.4.0` | MIT |
| `typescript` | `5.9.3` | Apache-2.0 |
| `vite` | `7.3.6` | MIT |
| `vitest` | `3.2.7` | MIT |

All frontend transitive versions and artifact integrity values are recorded in `pnpm-lock.yaml`.
Review the complete transitive license set before distributing installed modules or a compiled
frontend bundle rather than this source tree.

## Runtime tools and container images

| Component | Protected version/reference | Release treatment |
|---|---|---|
| llama.cpp | llama.cpp b10076, upstream commit `305ba51` | MIT; acquired separately; binary not redistributed |
| PostgreSQL/pgvector image | `docker.io/pgvector/pgvector:0.8.2-pg17-bookworm@sha256:feb68f4f15446397d8cac7f4fe48fe4586de83160d1fc48b46283312d1a33966` | pgvector uses the PostgreSQL license; pulled separately; image layers not redistributed |
| Redis image | `docker.io/library/redis:7.4.9-bookworm@sha256:a8f08480e1f88f2647fed492d1178c06abb0d0c1fbf02c682a61e2f483fb3954` | Redis 7.4.x is available under the operator's choice of RSALv2 or SSPLv1; pulled separately; image layers not redistributed |
| SearXNG image | `docker.io/searxng/searxng:2026.7.19-6da6eee26@sha256:b8ca38ba06eea544d7555e88321e212ddc0d5c3c7de055419cfb2e5c6bf30812` | AGPL-3.0; pulled separately; image layers not redistributed |

The repository pin establishes identity, not redistribution rights for an external image. Re-verify
upstream terms before redistribution of any image, binary, installed environment, or compiled
bundle. In particular, re-verify upstream terms before redistribution of Redis 7.4.9; Pensae Signal's
local pull-only use is not an assertion that repackaging is permitted.

Upstream license records reviewed for this inventory:

- llama.cpp MIT license: <https://github.com/ggml-org/llama.cpp/blob/master/LICENSE>
- pgvector PostgreSQL license: <https://github.com/pgvector/pgvector/blob/master/LICENSE>
- Redis version-license matrix: <https://redis.io/legal/licenses/>
- SearXNG AGPL-3.0 license: <https://github.com/searxng/searxng/blob/master/LICENSE>

These links identify upstream terms; they do not incorporate external license text into Pensae Signal or
grant permission to redistribute the pinned container layers.

## Publication audit tool

[Gitleaks](https://github.com/gitleaks/gitleaks), version 8.30.1, is an optional separately
installed local development/release tool under the MIT license. The repository does not vendor
its executable. Its distribution includes the upstream license; retain that license if
redistributing the tool. Installation is described in [README.md](README.md#installation); exact archive and binary
checksums are pinned in `scripts/release/publication.py`.

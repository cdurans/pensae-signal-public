from __future__ import annotations

import subprocess
from pathlib import Path


def _fake_uv(tmp_path: Path, version: str) -> Path:
    executable = tmp_path / "uv"
    executable.write_text(f"#!/usr/bin/bash\necho 'uv {version}'\n", encoding="utf-8")
    executable.chmod(0o700)
    return executable


def _fake_python(tmp_path: Path, version: str) -> Path:
    executable = tmp_path / f"python-{version}"
    executable.write_text(f"#!/usr/bin/bash\necho '{version}'\n", encoding="utf-8")
    executable.chmod(0o700)
    return executable


def _fake_node_home(tmp_path: Path, version: str, *, npm_cli: bool) -> Path:
    node_home = tmp_path / f"node-{version}-{'complete' if npm_cli else 'partial'}"
    node = node_home / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_text(f"#!/usr/bin/bash\necho 'v{version}'\n", encoding="utf-8")
    node.chmod(0o700)
    if npm_cli:
        npm = node_home / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
        npm.parent.mkdir(parents=True)
        npm.write_text("// pinned archive fixture\n", encoding="utf-8")
    return node_home


def test_uv_version_verifier_accepts_only_the_protected_pin(tmp_path: Path) -> None:
    verifier = Path("scripts/linux/verify_uv_version.sh").resolve()
    accepted = subprocess.run(  # noqa: S603 - fixed local test executables only
        (str(verifier), str(_fake_uv(tmp_path, "0.11.28"))),
        check=False,
        capture_output=True,
        text=True,
    )
    rejected = subprocess.run(  # noqa: S603 - fixed local test executables only
        (str(verifier), str(_fake_uv(tmp_path, "0.11.29"))),
        check=False,
        capture_output=True,
        text=True,
    )

    assert accepted.returncode == 0
    assert rejected.returncode == 2
    assert "requires uv 0.11.28" in rejected.stderr


def test_bootstrap_requires_exact_native_fedora_release_only() -> None:
    source = Path("scripts/linux/bootstrap.sh").read_text(encoding="utf-8")

    assert 'grep -qx "Fedora release 44 (Forty Four)" /etc/fedora-release' in source
    assert "kernel/osrelease" not in source
    assert "mv --no-clobber --no-target-directory" in source
    assert "refusing to overwrite" in source


def test_python_version_verifier_accepts_only_the_protected_pin(tmp_path: Path) -> None:
    verifier = Path("scripts/linux/verify_python_version.sh").resolve()
    accepted = subprocess.run(  # noqa: S603 - fixed local test executables only
        (str(verifier), str(_fake_python(tmp_path, "3.13.14"))),
        check=False,
        capture_output=True,
        text=True,
    )
    rejected = subprocess.run(  # noqa: S603 - fixed local test executables only
        (str(verifier), str(_fake_python(tmp_path, "3.13.13"))),
        check=False,
        capture_output=True,
        text=True,
    )

    assert accepted.returncode == 0
    assert rejected.returncode == 2
    assert "requires Python 3.13.14" in rejected.stderr


def test_node_toolchain_verifier_requires_exact_node_and_archive_npm(tmp_path: Path) -> None:
    verifier = Path("scripts/linux/verify_node_toolchain.sh").resolve()
    exact = _fake_node_home(tmp_path, "24.18.0", npm_cli=True)
    wrong = _fake_node_home(tmp_path, "24.18.1", npm_cli=True)
    partial = _fake_node_home(tmp_path, "24.18.0", npm_cli=False)

    results = [
        subprocess.run(  # noqa: S603 - fixed local test executables only
            (str(verifier), str(node_home)),
            check=False,
            capture_output=True,
            text=True,
        )
        for node_home in (exact, wrong, partial)
    ]

    assert [result.returncode for result in results] == [0, 2, 2]
    assert all("requires Node 24.18.0" in result.stderr for result in results[1:])

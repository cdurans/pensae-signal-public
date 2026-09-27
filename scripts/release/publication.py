"""Local publication secret gate: all reachable Git objects and the proposed files.

Reports contain locations and rule IDs only. No credential validation or upload.
Office ZIP members and joined XML text are scanned, including commit metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree

VERSION = "8.30.1"
ARCHIVE_SHA256 = "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
BINARY_SHA256 = "88f91962aa2f93ac6ab281d553b9e125f5197bbbce38f9f2437f7299c32e5509"
URL = f"https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}/gitleaks_{VERSION}_linux_x64.tar.gz"
MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
MAX_MEMBERS = 20_000
SENSITIVE = re.compile(
    r"(?i)(?:^|/)(?:\.env(?:\..*)?|\.netrc|\.npmrc|\.pypirc|id_(?:rsa|ed25519|dsa|ecdsa)"
    r"|credentials(?:\..*)?|service-account[^/]*\.json|secrets?\.[^/]+|cookies\.txt"
    r"|auth\.json|storage-state[^/]*\.json)$"
    r"|(?:^|/)(?:\.aws|\.ssh|\.gnupg|\.kube|\.private|\.audit|exports)/"
    r"|\.(?:pem|key|p12|pfx|kdbx|keystore|jks|ovpn|tfstate|har|cast|sqlite3?|db|sql)(?:\..*)?$"
)
EXAMPLES = {".env.example", "secrets.example.json", "secrets.example.toml"}


def git(root: Path, *args: str, input_data: bytes | None = None) -> bytes:
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("Git is required")
    return subprocess.run(  # noqa: S603 - fixed Git operations, never a shell
        [executable, "-C", str(root), *args],
        check=True,
        input=input_data,
        capture_output=True,
        timeout=120,
        env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"},
    ).stdout


def read_object(root: Path, kind: str, object_id: str) -> bytes:
    if int(git(root, "cat-file", "-s", object_id)) > MAX_FILE:
        raise ValueError("oversized Git object")
    return git(root, "cat-file", kind, object_id)


def read_regular(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        details = os.fstat(stream.fileno())
        if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_FILE:
            raise ValueError("publication input must be a bounded regular file")
        data = stream.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise ValueError("publication input grew beyond the size limit")
        return data


def install(root: Path, archive: Path | None = None) -> None:
    if archive is None:
        with urllib.request.urlopen(URL, timeout=60) as response:  # noqa: S310 - pinned HTTPS URL
            payload = response.read(MAX_FILE + 1)
    else:
        payload = read_regular(archive)
    if hashlib.sha256(payload).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("scanner archive checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as package:
        member = package.getmember("gitleaks")
        if not member.isfile() or member.size > MAX_FILE:
            raise ValueError("invalid scanner member")
        stream = package.extractfile(member)
        if stream is None:
            raise ValueError("missing scanner member")
        binary = stream.read(MAX_FILE + 1)
    if hashlib.sha256(binary).hexdigest() != BINARY_SHA256:
        raise ValueError("scanner binary checksum mismatch")
    destination = root / ".tools" / "gitleaks" / VERSION / "gitleaks"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
        temporary.write(binary)
        temporary.flush()
        os.fchmod(temporary.fileno(), 0o700)
    os.replace(temporary.name, destination)


class Corpus:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.origins: dict[str, str] = {}
        self.total = 0
        self.seen: set[tuple[str, str]] = set()

    def add(self, data: bytes, origin: str, depth: int = 0) -> None:
        if len(data) > MAX_FILE or self.total + len(data) > MAX_TOTAL:
            raise ValueError("scan size limit reached; review the oversized input separately")
        if len(self.origins) >= MAX_MEMBERS:
            raise ValueError("scan member limit reached")
        unsupported_suffixes = (".gz", ".tgz", ".tar", ".bz2", ".xz", ".7z", ".rar", ".zst")
        unsupported_magic = (
            b"\x1f\x8b",
            b"BZh",
            b"\xfd7zXZ\x00",
            b"7z\xbc\xaf\x27\x1c",
            b"Rar!",
            b"\x28\xb5\x2f\xfd",
        )
        if (
            origin.lower().endswith(unsupported_suffixes)
            or data.startswith(unsupported_magic)
            or data[257:262] == b"ustar"
        ):
            raise ValueError("unsupported compressed archive; unpack and review before publication")
        digest = hashlib.sha256(data).hexdigest()
        is_archive = data.startswith(b"PK") or origin.lower().endswith(
            (".docx", ".xlsx", ".pptx", ".zip")
        )
        is_xml = origin.lower().endswith((".xml", ".rels")) or data.lstrip().startswith(
            (b"<?xml", b"<w:", b"<a:")
        )
        kind = "archive" if is_archive else "xml" if is_xml else "plain"
        identity = (digest, kind)
        if identity in self.seen:
            return
        self.seen.add(identity)
        self.total += len(data)
        name = f"item-{len(self.origins):06}.txt"
        (self.directory / name).write_bytes(data)
        self.origins[name] = origin
        if is_archive:
            if depth >= 3:
                raise ValueError("archive depth limit reached")
            with zipfile.ZipFile(io.BytesIO(data)) as package:
                for member in package.infolist():
                    if member.is_dir():
                        continue
                    if member.file_size > MAX_FILE or member.flag_bits & 1:
                        raise ValueError("oversized or encrypted archive member")
                    self.add(package.read(member), f"{origin}!{member.filename}", depth + 1)
        elif is_xml:
            declarations = data.replace(b"\x00", b"").upper()
            if b"<!DOCTYPE" in declarations or b"<!ENTITY" in declarations:
                raise ValueError("XML entities are not supported")
            document = ElementTree.fromstring(data)  # noqa: S314 - entity declarations refused above
            # Joining runs detects secrets split by Word/Office formatting.
            joined = "".join(document.itertext()).encode()
            if joined != data:
                self.add(joined, f"{origin} [joined XML]", depth)


def collect(root: Path, corpus: Corpus) -> tuple[int, int, list[str]]:
    if git(root, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise ValueError("a complete clone is required")
    if any(
        name.endswith(".promisor")
        for name in git(root, "config", "--list", "--name-only").decode().splitlines()
    ):
        raise ValueError("partial clones are not supported")
    return collect_complete(root, corpus)


def collect_complete(root: Path, corpus: Corpus) -> tuple[int, int, list[str]]:
    commits = git(root, "rev-list", "--all", "HEAD").decode().splitlines()
    for line in (
        git(root, "for-each-ref", "--format=%(objecttype) %(objectname)").decode().splitlines()
    ):
        kind, object_id = line.split()
        if kind == "tag":
            corpus.add(read_object(root, "tag", object_id), f"tag {object_id}")
    blobs: dict[str, str] = {}
    paths: set[str] = set()
    for commit in commits:
        corpus.add(read_object(root, "commit", commit), f"commit {commit}")
        for entry in git(root, "ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
            if not entry:
                continue
            header, raw_path = entry.split(b"\t", 1)
            mode, kind, object_id = header.decode().split()
            path = raw_path.decode()
            if kind != "blob" or mode == "120000":
                raise ValueError("submodules and symlinks require separate review")
            paths.add(path)
            blobs.setdefault(object_id, path)
    # Also cover objects reached through non-commit refs and nested annotated tags.
    reachable = git(root, "rev-list", "--objects", "--no-object-names", "--all", "HEAD")
    for line in git(root, "cat-file", "--batch-check", input_data=reachable).decode().splitlines():
        object_id, kind, size = line.split()
        if kind == "blob":
            if int(size) > MAX_FILE:
                raise ValueError("oversized reachable blob")
            blobs.setdefault(object_id, "[additional reachable blob]")
        elif kind == "tag":
            corpus.add(read_object(root, "tag", object_id), f"tag {object_id}")
    corpus.add(git(root, "for-each-ref", "--format=%(refname)"), "local ref names")
    for object_id, path in blobs.items():
        corpus.add(read_object(root, "blob", object_id), f"history {object_id}:{path}")
    working = set(
        git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0")
    )
    for raw_path in sorted(working - {b""}):
        path = raw_path.decode()
        paths.add(path)
        candidate = root / path
        if candidate.is_symlink():
            raise ValueError("working-tree symlinks require separate review")
        if candidate.exists():
            corpus.add(read_regular(candidate), f"working {path}")
    # The index may differ from both HEAD and the working file (partial staging).
    for entry in git(root, "ls-files", "-s", "-z").split(b"\0"):
        if not entry:
            continue
        header, raw_path = entry.split(b"\t", 1)
        mode, object_id, stage = header.decode().split()
        if stage != "0" or (mode != "100644" and mode != "100755"):
            raise ValueError("unmerged or unsupported index entry")
        corpus.add(read_object(root, "blob", object_id), f"index {raw_path.decode()}")
    suspicious = sorted(p for p in paths if SENSITIVE.search(p) and Path(p).name not in EXAMPLES)
    return len(commits), len(blobs), suspicious


def scan(root: Path) -> int:
    binary = root / ".tools" / "gitleaks" / VERSION / "gitleaks"
    if not binary.is_file() or hashlib.sha256(read_regular(binary)).hexdigest() != BINARY_SHA256:
        raise ValueError("install the pinned scanner using make install-secret-scanner")
    with tempfile.TemporaryDirectory(prefix="pensae-secret-scan-") as temporary:
        directory = Path(temporary)
        payload = directory / "corpus"
        payload.mkdir(mode=0o700)
        corpus = Corpus(payload)
        commits, blobs, suspicious = collect(root, corpus)
        report = directory / "report.json"
        ignore = directory / "empty-ignore"
        ignore.touch(mode=0o600)
        result = subprocess.run(  # noqa: S603 - verified binary and fixed options
            [
                str(binary),
                "dir",
                str(payload),
                "--config",
                str(root / ".gitleaks.toml"),
                "--redact=100",
                "--ignore-gitleaks-allow",
                "--gitleaks-ignore-path",
                str(ignore),
                "--max-decode-depth",
                "5",
                "--max-archive-depth",
                "3",
                "--no-banner",
                "--report-format",
                "json",
                "--report-path",
                str(report),
            ],
            capture_output=True,
            timeout=180,
            env={k: v for k, v in os.environ.items() if not k.startswith("GITLEAKS_")},
        )
        if result.returncode not in (0, 1) or not report.is_file():
            raise RuntimeError("secret scanner did not complete; output withheld")
        findings = json.loads(report.read_bytes())
        if not isinstance(findings, list) or bool(findings) != (result.returncode == 1):
            raise RuntimeError("inconsistent scanner result")
        for item in findings:
            location = corpus.origins.get(Path(item["File"]).name, "unmapped scan location")
            # Deliberately omit Match, Secret, line contents, authors and commit messages.
            rule = re.sub(r"[^a-zA-Z0-9_-]", "", item["RuleID"])
            print(f"FAIL {rule}: {json.dumps(location)}")
        for path in suspicious:
            print(f"FAIL sensitive filename: {json.dumps(path)}")
        print(
            f"Scanned {commits} commits, {blobs} historical blobs, index and working files; "
            f"{len(corpus.origins)} expanded items, {len(findings) + len(suspicious)} findings."
        )
        return int(bool(findings or suspicious))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "scan"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "install":
            install(args.root.resolve(), args.archive)
            print(f"Verified Gitleaks {VERSION} installed locally.")
            return 0
        return scan(args.root.resolve())
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
        zipfile.BadZipFile,
        tarfile.TarError,
        ElementTree.ParseError,
        KeyError,
        TypeError,
    ):
        print(
            "Publication scan could not complete safely. Check tool installation, full Git "
            "history and archive validity/limits; no scanner payload was displayed."
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

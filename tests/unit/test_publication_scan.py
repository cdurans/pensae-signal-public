from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path

import pytest
from scripts.release import publication


def commit(root: Path) -> None:
    publication.git(root, "add", ".")
    publication.git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )


def test_deleted_history_partial_index_and_office_runs_are_scanned(tmp_path: Path) -> None:
    publication.git(tmp_path, "init", "-q")
    secret = "ghp_" + "aB9xY7zQ5rT3mN1kL8pO6iU4vW2cE0dF7gH5"
    deleted = tmp_path / "removed.txt"
    deleted.write_text(secret)
    commit(tmp_path)
    deleted.unlink()
    staged = tmp_path / "staged.txt"
    staged.write_text("index-only value")
    commit(tmp_path)
    staged.write_text("partial staging value")
    publication.git(tmp_path, "add", "staged.txt")
    staged.write_text("working value")
    secret = secret + "Office"
    with zipfile.ZipFile(tmp_path / "test.docx", "w") as package:
        package.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:p xmlns:w="urn:test">'
            f"<w:r><w:t>{secret[:12]}</w:t></w:r>"
            f"<w:r><w:t>{secret[12:]}</w:t></w:r></w:p>",
        )
    corpus_dir = tmp_path / ".ignored-corpus"
    (tmp_path / ".gitignore").write_text(".ignored-corpus/\n")
    corpus_dir.mkdir()
    corpus = publication.Corpus(corpus_dir)
    commits, blobs, paths = publication.collect(tmp_path, corpus)
    assert commits == 2 and blobs == 2 and not paths
    contents = [p.read_bytes() for p in corpus_dir.iterdir()]
    assert secret.encode() in contents
    assert b"partial staging value" in contents and b"working value" in contents
    assert any("joined XML" in origin for origin in corpus.origins.values())


def test_sensitive_filename_force_add_is_caught(tmp_path: Path) -> None:
    publication.git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text(".env*\n.scan/\n")
    commit(tmp_path)
    (tmp_path / ".env.production").write_text("private configuration")
    publication.git(tmp_path, "add", "-f", ".env.production")
    directory = tmp_path / ".scan"
    directory.mkdir()
    assert publication.collect(tmp_path, publication.Corpus(directory))[2] == [".env.production"]


def test_archives_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    corpus = publication.Corpus(tmp_path)
    with pytest.raises(zipfile.BadZipFile):
        corpus.add(b"invalid package", "bad.docx")
    with pytest.raises(ValueError, match="entities"):
        corpus.add(b'<?xml version="1.0"?><!DOCTYPE foo><foo/>', "unsafe.xml")
    monkeypatch.setattr(publication, "MAX_FILE", 8)
    with pytest.raises(ValueError, match="size"):
        corpus.add(b"too many bytes", "large.txt")


def test_unknown_scanner_cannot_run(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="install the pinned"):
        publication.scan(tmp_path)


def test_scanner_errors_never_print_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*args: object) -> int:
        raise subprocess.CalledProcessError(2, "scanner", output="PRIVATE_SENTINEL")

    monkeypatch.setattr(publication, "scan", fail)
    monkeypatch.setattr("sys.argv", ["publication", "scan", "--root", str(tmp_path)])
    assert publication.main() == 2
    assert "PRIVATE_SENTINEL" not in capsys.readouterr().out


def test_office_default_namespace_without_declaration_is_joined(tmp_path: Path) -> None:
    corpus = publication.Corpus(tmp_path)
    corpus.add(
        b'<p xmlns="urn:word"><r><t>split</t></r><r><t>-value</t></r></p>',
        "document.docx!word/document.xml",
    )
    assert b"split-value" in [p.read_bytes() for p in tmp_path.iterdir()]


def test_deduplication_cannot_hide_invalid_archive_type(tmp_path: Path) -> None:
    corpus = publication.Corpus(tmp_path)
    corpus.add(b"invalid package", "safe.txt")
    with pytest.raises(zipfile.BadZipFile):
        corpus.add(b"invalid package", "broken.docx")


@pytest.mark.parametrize(
    "data,origin",
    [
        (b"\x1f\x8bmalformed", "concealed.txt"),
        (b"not really tar", "backup.tar"),
        (b"7z\xbc\xaf\x27\x1cdata", "opaque.bin"),
    ],
)
def test_unsupported_archives_fail_closed(tmp_path: Path, data: bytes, origin: str) -> None:
    with pytest.raises(ValueError, match="unsupported compressed"):
        publication.Corpus(tmp_path).add(data, origin)


def test_working_input_refuses_fifo_and_oversize_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular file"):
        publication.read_regular(fifo)
    large = tmp_path / "large"
    large.write_bytes(b"123456789")
    monkeypatch.setattr(publication, "MAX_FILE", 8)
    with pytest.raises(ValueError, match="regular file"):
        publication.read_regular(large)


def test_oversized_index_only_blob_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publication.git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text(".scan/\n")
    target = tmp_path / "candidate.txt"
    target.write_text("small initial content")
    commit(tmp_path)
    target.write_bytes(b"a" * 2000)
    publication.git(tmp_path, "add", "candidate.txt")
    target.write_text("small working content")
    monkeypatch.setattr(publication, "MAX_FILE", 512)
    directory = tmp_path / ".scan"
    directory.mkdir()
    with pytest.raises(ValueError, match="oversized Git object"):
        publication.collect(tmp_path, publication.Corpus(directory))

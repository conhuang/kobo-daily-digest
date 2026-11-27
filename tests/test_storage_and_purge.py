from __future__ import annotations

from datetime import date
import json
from pathlib import Path

from kobo_digest.storage import parse_digest_filename, purge_old_digests, write_manifest_and_index


def test_parse_digest_filename():
    assert parse_digest_filename("2026-11-26-HN.epub") == (date(2026, 11, 26), "HN")
    assert parse_digest_filename("2026-11-26-TLDR.epub") == (date(2026, 11, 26), "TLDR")
    assert parse_digest_filename("notes.epub") is None
    assert parse_digest_filename("2026-11-26-HN.txt") is None
    assert parse_digest_filename("2026-99-99-HN.epub") is None


def test_purge_old_digests_keeps_four_day_window(tmp_path: Path):
    # Reference day: 2026-11-26 with retention_days=4
    # Keeps: Nov 26, 25, 24, 23 (4 days). Purges: Nov 22 and older.
    filenames = [
        "2026-11-26-HN.epub",
        "2026-11-25-HN.epub",
        "2026-11-24-HN.epub",
        "2026-11-23-HN.epub",
        "2026-11-22-HN.epub",  # 4 days old -> purge
        "2026-11-20-TLDR.epub",  # 6 days old -> purge
        "my-other-book.epub",  # unrelated file -> never touched
    ]
    for name in filenames:
        (tmp_path / name).write_bytes(b"fake-epub-content")

    removed = purge_old_digests(tmp_path, retention_days=4, today=date(2026, 11, 26))
    removed_names = sorted(p.name for p in removed)
    assert removed_names == ["2026-11-20-TLDR.epub", "2026-11-22-HN.epub"]

    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == [
        "2026-11-23-HN.epub",
        "2026-11-24-HN.epub",
        "2026-11-25-HN.epub",
        "2026-11-26-HN.epub",
        "my-other-book.epub",
    ]


def test_write_manifest_and_index(tmp_path: Path):
    (tmp_path / "2026-11-25-HN.epub").write_bytes(b"day1")
    (tmp_path / "2026-11-26-HN.epub").write_bytes(b"day2-longer")
    (tmp_path / "unrelated.epub").write_bytes(b"ignore-me")

    manifest_path = write_manifest_and_index(tmp_path, retention_days=4)
    assert manifest_path.exists()
    assert (tmp_path / "index.html").exists()

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["retention_days"] == 4
    assert [d["filename"] for d in data["digests"]] == [
        "2026-11-26-HN.epub",
        "2026-11-25-HN.epub",
    ]
    assert data["digests"][0]["size_bytes"] == len(b"day2-longer")
    assert len(data["digests"][0]["sha256"]) == 64

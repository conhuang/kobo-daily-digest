from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import html
import json
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple

from .models import Digest

logger = logging.getLogger(__name__)

DIGEST_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-([A-Za-z0-9_-]+)\.epub$")


def parse_digest_filename(filename: str) -> Optional[Tuple[date, str]]:
    """Parse `YYYY-MM-DD-SOURCE.epub` into (date, source_code), or None if not matching."""
    match = DIGEST_FILENAME_RE.match(filename)
    if not match:
        return None
    date_str, source_code = match.groups()
    try:
        parsed_date = date.fromisoformat(date_str)
    except ValueError:
        return None
    return parsed_date, source_code


def purge_old_digests(
    out_dir: Path,
    retention_days: int = 4,
    today: Optional[date] = None,
) -> List[Path]:
    """Delete digest EPUBs older than `retention_days` (keeps today + past retention_days-1 days).

    Example: retention_days=4 on 2026-11-26 keeps Nov 26, 25, 24, 23 and purges <= Nov 22.
    """
    if retention_days < 1:
        raise ValueError("retention_days must be >= 1")

    ref_today = today or date.today()
    cutoff = ref_today - timedelta(days=retention_days)
    removed: List[Path] = []

    if not out_dir.exists():
        return removed

    for entry in sorted(out_dir.iterdir()):
        if not entry.is_file():
            continue
        parsed = parse_digest_filename(entry.name)
        if not parsed:
            continue
        file_date, _ = parsed
        if file_date <= cutoff:
            logger.info("Purging expired digest: %s (date=%s <= cutoff=%s)", entry.name, file_date, cutoff)
            entry.unlink()
            removed.append(entry)

    return removed


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest_and_index(
    out_dir: Path,
    retention_days: int = 4,
    updated_digests: Optional[Dict[str, Digest]] = None,
) -> Path:
    """Scan `out_dir` for active digest EPUBs and write `manifest.json` + `index.html`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    updated_digests = updated_digests or {}

    # Preserve existing metadata (title/article_count) from prior manifest if present.
    prior_meta: Dict[str, Dict[str, Any]] = {}
    manifest_path = out_dir / "manifest.json"
    if manifest_path.exists():
        try:
            old = json.loads(manifest_path.read_text(encoding="utf-8"))
            for item in old.get("digests", []):
                if isinstance(item, dict) and "filename" in item:
                    prior_meta[item["filename"]] = item
        except Exception:
            pass

    entries: List[Dict[str, Any]] = []
    for path in sorted(out_dir.iterdir(), reverse=True):
        if not path.is_file():
            continue
        parsed = parse_digest_filename(path.name)
        if not parsed:
            continue
        file_date, source_code = parsed

        digest_obj = updated_digests.get(path.name)
        old_item = prior_meta.get(path.name, {})
        if digest_obj is not None:
            title = digest_obj.title
            article_count = len(digest_obj.articles)
        else:
            title = old_item.get("title") or f"{source_code} Daily — {file_date.isoformat()}"
            article_count = old_item.get("article_count")

        entries.append(
            {
                "filename": path.name,
                "source": source_code,
                "date": file_date.isoformat(),
                "title": title,
                "article_count": article_count,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
                "url": path.name,
            }
        )

    manifest = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "retention_days": retention_days,
        "digests": entries,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # Also write a minimal HTML index for easy browsing/debugging on GitHub Pages.
    rows = []
    for item in entries:
        size_kb = max(1, round(item["size_bytes"] / 1024))
        rows.append(
            f'<li><a href="{html.escape(item["url"])}">{html.escape(item["title"])}</a> '
            f'<small>({size_kb} KB)</small></li>'
        )
    list_html = "\n    ".join(rows) if rows else "<li>No digests available yet.</li>"
    index_html = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Kobo Daily Digests</title>
  <style>
    body {{ font-family: -apple-system, sans-serif; max-width: 640px; margin: 2rem auto; padding: 0 1rem; line-height: 1.6; }}
    li {{ margin-bottom: 0.5rem; }}
    code {{ background: #f3f3f3; padding: 0.15rem 0.35rem; border-radius: 4px; }}
  </style>
</head>
<body>
  <h1>Kobo Daily Digests</h1>
  <p>Last updated: {manifest["generated_at"]} · Retention: {retention_days} days · <a href="manifest.json">manifest.json</a></p>
  <ul>
    {list_html}
  </ul>
</body>
</html>
"""
    (out_dir / "index.html").write_text(index_html, encoding="utf-8")
    return manifest_path

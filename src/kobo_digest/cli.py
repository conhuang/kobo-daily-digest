from __future__ import annotations

import argparse
from datetime import date
import logging
from pathlib import Path
import sys
from typing import Dict, List, Optional

from .epub_builder import build_epub
from .fetcher_bbc_zh import fetch_bbc_zh_digest
from .fetcher_hn import fetch_hn_digest
from .fetcher_ruanyf import fetch_ruanyf_digest
from .fetcher_tldr import fetch_tldr_digest
from .models import Digest
from .storage import purge_old_digests, write_manifest_and_index

logger = logging.getLogger(__name__)

SUPPORTED_SOURCES = ("hn", "tldr", "bbc-zh", "ruanyf")


def cmd_build(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    target_date = date.fromisoformat(args.date) if args.date else date.today()
    sources: List[str] = [s.strip().lower() for s in args.sources]

    updated: Dict[str, Digest] = {}
    for source in sources:
        digest: Optional[Digest]
        if source == "hn":
            digest = fetch_hn_digest(
                digest_date=target_date,
                max_articles=args.max_articles,
                candidate_pool=args.candidate_pool,
            )
        elif source == "tldr":
            digest = fetch_tldr_digest(
                digest_date=target_date,
                max_articles=max(args.max_articles, 20),
                exact_date=bool(args.date),
            )
        elif source == "bbc-zh":
            digest = fetch_bbc_zh_digest(
                digest_date=target_date,
                max_articles=min(args.max_articles, 10),
            )
        elif source == "ruanyf":
            digest = fetch_ruanyf_digest(
                digest_date=target_date,
                retention_days=args.retention_days,
            )
        else:
            logger.error("Unknown source %r (supported: %s)", source, ", ".join(SUPPORTED_SOURCES))
            return 2

        if digest is None or not digest.articles:
            logger.info("No digest generated for source=%s on %s; skipping.", source, target_date)
            continue

        epub_path = out_dir / digest.filename
        build_epub(digest, epub_path)
        logger.info("Wrote %s (%d articles, %d bytes)", epub_path, len(digest.articles), epub_path.stat().st_size)
        updated[digest.filename] = digest

    removed = purge_old_digests(out_dir, retention_days=args.retention_days, today=target_date)
    if removed:
        logger.info("Purged %d expired digest(s).", len(removed))

    manifest_path = write_manifest_and_index(
        out_dir,
        retention_days=args.retention_days,
        updated_digests=updated,
    )
    logger.info("Updated manifest: %s", manifest_path)
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    target_date = date.fromisoformat(args.date) if args.date else date.today()
    removed = purge_old_digests(out_dir, retention_days=args.retention_days, today=target_date)
    write_manifest_and_index(out_dir, retention_days=args.retention_days)
    logger.info("Purged %d expired digest(s) from %s.", len(removed), out_dir)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kobo-digest",
        description="Build daily EPUB digests (HN, TLDR Tech, BBC 中文, 阮一峰周刊) for Kobo with 4-day auto-purge.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="Fetch sources, build daily EPUB(s), purge old ones, update manifest.")
    p_build.add_argument(
        "--source",
        dest="sources",
        action="append",
        choices=SUPPORTED_SOURCES,
        help="Source(s) to build (can be repeated; defaults to all supported sources).",
    )
    p_build.add_argument("--out", default="dist/digests", help="Output directory for EPUBs and manifest.json.")
    p_build.add_argument("--max-articles", type=int, default=15, help="Max readable articles per digest (default: 15).")
    p_build.add_argument("--candidate-pool", type=int, default=40, help="Candidate HN stories to inspect (default: 40).")
    p_build.add_argument("--retention-days", type=int, default=4, help="Days of digests to keep (default: 4).")
    p_build.add_argument("--date", default=None, help="Override digest date (YYYY-MM-DD, defaults to today).")
    p_build.set_defaults(func=cmd_build)

    p_purge = sub.add_parser("purge", help="Purge expired digests and refresh manifest.json.")
    p_purge.add_argument("--out", default="dist/digests", help="Directory containing digest EPUBs.")
    p_purge.add_argument("--retention-days", type=int, default=4, help="Days of digests to keep (default: 4).")
    p_purge.add_argument("--date", default=None, help="Reference date (YYYY-MM-DD, defaults to today).")
    p_purge.set_defaults(func=cmd_purge)

    return parser


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "build" and not args.sources:
        args.sources = list(SUPPORTED_SOURCES)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

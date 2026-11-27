from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
import re
from typing import List, Optional, Tuple
from xml.etree import ElementTree as ET

from lxml import etree, html as lxml_html
import requests

from .article_extractor import USER_AGENT, count_text_metrics, sanitize_fragment_to_xhtml
from .models import Article, Digest

logger = logging.getLogger(__name__)

RUANYF_ATOM_URL = "https://www.ruanyifeng.com/blog/atom.xml"
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
SKIP_SECTIONS = {"封面图", "图片", "往年回顾", "订阅"}
MIN_SECTION_CJK_CHARS = 35


def parse_ruanyf_atom_weekly_entries(atom_xml: bytes | str) -> List[Tuple[str, str, date, str]]:
    """Return [(title, url, pub_date, content_html)] for 科技爱好者周刊 entries, newest first."""
    root = ET.fromstring(atom_xml)
    entries: List[Tuple[str, str, date, str]] = []

    for entry in root.findall("a:entry", ATOM_NS):
        title = (entry.findtext("a:title", "", ATOM_NS) or "").strip()
        if "周刊" not in title:
            continue

        link_el = entry.find("a:link", ATOM_NS)
        url = link_el.attrib.get("href", "").strip() if link_el is not None else ""

        raw_pub = (
            entry.findtext("a:published", "", ATOM_NS)
            or entry.findtext("a:updated", "", ATOM_NS)
            or ""
        ).strip()
        match = re.match(r"^(\d{4}-\d{2}-\d{2})", raw_pub)
        if not match:
            continue
        pub_date = date.fromisoformat(match.group(1))

        content_html = (entry.findtext("a:content", "", ATOM_NS) or "").strip()
        if not content_html:
            continue

        entries.append((title, url, pub_date, content_html))

    entries.sort(key=lambda row: row[2], reverse=True)
    return entries


def split_weekly_html_into_chapters(
    content_html: str,
    issue_url: str,
    issue_title: str,
) -> List[Article]:
    """Split a Ruan Yifeng weekly issue's HTML into one EPUB chapter per <h2> section."""
    wrapper = lxml_html.fragment_fromstring(content_html, create_parent="div")

    sections: List[Tuple[str, List[etree._Element]]] = []
    current_title: Optional[str] = None
    current_nodes: List[etree._Element] = []

    for child in list(wrapper):
        tag = child.tag.lower() if isinstance(child.tag, str) else ""
        if tag == "h2":
            if current_title is not None and current_nodes:
                sections.append((current_title, current_nodes))
            current_title = " ".join("".join(child.itertext()).split()).strip()
            current_nodes = []
        else:
            if current_title is not None:
                current_nodes.append(child)

    if current_title is not None and current_nodes:
        sections.append((current_title, current_nodes))

    articles: List[Article] = []
    for sec_title, nodes in sections:
        if sec_title in SKIP_SECTIONS:
            continue

        raw_fragment = "\n".join(
            etree.tostring(node, method="html", encoding="unicode") for node in nodes
        )
        body_xhtml, _ = sanitize_fragment_to_xhtml(raw_fragment)
        if not body_xhtml:
            continue

        latin_words, cjk_chars = count_text_metrics(body_xhtml)
        if cjk_chars < MIN_SECTION_CJK_CHARS:
            continue

        articles.append(
            Article(
                title=sec_title,
                url=issue_url,
                body_xhtml=body_xhtml,
                source_name="阮一峰科技爱好者周刊",
                author="阮一峰",
                domain="ruanyifeng.com",
                word_count=latin_words,
                cjk_char_count=cjk_chars,
            )
        )

    # Fallback: if no <h2> headings matched, keep the full issue as a single chapter.
    if not articles:
        body_xhtml, _ = sanitize_fragment_to_xhtml(content_html)
        latin_words, cjk_chars = count_text_metrics(body_xhtml)
        if cjk_chars >= MIN_SECTION_CJK_CHARS:
            articles.append(
                Article(
                    title=issue_title,
                    url=issue_url,
                    body_xhtml=body_xhtml,
                    source_name="阮一峰科技爱好者周刊",
                    author="阮一峰",
                    domain="ruanyifeng.com",
                    word_count=latin_words,
                    cjk_char_count=cjk_chars,
                )
            )

    return articles


def fetch_ruanyf_digest(
    digest_date: Optional[date] = None,
    retention_days: int = 4,
) -> Optional[Digest]:
    """Fetch the latest 阮一峰《科技爱好者周刊》 if published within the active retention window."""
    target_date = digest_date or date.today()
    session = requests.Session()

    logger.info("Fetching 阮一峰科技爱好者周刊 Atom feed...")
    resp = session.get(RUANYF_ATOM_URL, timeout=15, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()

    entries = parse_ruanyf_atom_weekly_entries(resp.content)
    cutoff = target_date - timedelta(days=retention_days)
    max_date = target_date + timedelta(days=1)  # allow UTC / UTC+8 date ahead of local time

    chosen = None
    for entry in entries:
        _, _, pub_date, _ = entry
        if cutoff < pub_date <= max_date:
            chosen = entry
            break

    if chosen is None:
        logger.info(
            "No 阮一峰周刊 issue within retention window (%s..%s); latest is %s.",
            cutoff,
            target_date,
            entries[0][2] if entries else "none",
        )
        return None

    issue_title, issue_url, pub_date, content_html = chosen
    chapters = split_weekly_html_into_chapters(content_html, issue_url, issue_title)
    if not chapters:
        logger.warning("Parsed 0 readable chapters from %s.", issue_title)
        return None

    logger.info("Built 阮一峰周刊 digest (%s) with %d chapters.", pub_date, len(chapters))
    return Digest(
        source_code="RUANYF",
        source_title="阮一峰科技爱好者周刊",
        digest_date=pub_date,
        articles=chapters,
        language="zh-Hans",
        custom_title=issue_title,
    )

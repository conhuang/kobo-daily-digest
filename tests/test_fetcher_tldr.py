from __future__ import annotations

from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET
import zipfile

from kobo_digest.article_extractor import clean_url
from kobo_digest.epub_builder import build_epub
from kobo_digest.fetcher_tldr import (
    parse_rss_issue_links,
    parse_tldr_issue_html,
    strip_non_bmp_and_emoji,
)
from kobo_digest.models import Article, Digest

SAMPLE_RSS = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item><title>Issue 2</title><link>https://tldr.tech/tech/2026-11-26</link></item>
  <item><title>Issue 1</title><link>https://tldr.tech/tech/2026-11-25</link></item>
</channel></rss>
"""

SAMPLE_ISSUE_HTML = """\
<html><body>
  <section>
    <header><h3>Together With</h3></header>
    <article>
      <a href="https://sponsor.example.com/?utm_source=tldr"><h3>Buy Our Tool (Sponsor)</h3></a>
      <div class="newsletter-html">Ad copy here.</div>
    </article>
  </section>
  <section>
    <header><h3>📱 Big Tech &amp; Startups</h3></header>
    <article>
      <a href="https://example.com/story1?id=42&amp;utm_source=tldrnewsletter"><h3>Rocket Launch News (4 minute read)</h3></a>
      <div class="newsletter-html">A concise summary of the rocket launch.</div>
    </article>
  </section>
  <section>
    <header><h3>💻 Programming, Design &amp; Data Science</h3></header>
    <article>
      <a href="https://advertise.tldr.tech/book"><h3>Advertise with us (Sponsor)</h3></a>
      <div class="newsletter-html">Sponsor pitch.</div>
    </article>
    <article>
      <a href="https://blog.example.org/post?utm_source=tldrnewsletter"><h3>Clean Engineering (7 minute read)</h3></a>
      <div class="newsletter-html">Summary about software engineering craft.</div>
    </article>
  </section>
</body></html>
"""


def test_parse_rss_issue_links():
    issues = parse_rss_issue_links(SAMPLE_RSS)
    assert issues == [
        (date(2026, 11, 26), "https://tldr.tech/tech/2026-11-26"),
        (date(2026, 11, 25), "https://tldr.tech/tech/2026-11-25"),
    ]


def test_clean_url_strips_utm_and_unescapes():
    raw = "https://example.com/a?id=7&amp;utm_source=tldrnewsletter&amp;reflink=abc"
    assert clean_url(raw) == "https://example.com/a?id=7"


def test_strip_non_bmp_and_emoji():
    assert strip_non_bmp_and_emoji("📱 Big Tech & Startups") == "Big Tech & Startups"
    assert strip_non_bmp_and_emoji("⚡ Quick Links") == "Quick Links"


def test_parse_tldr_issue_filters_sponsors_and_extracts_items():
    items = parse_tldr_issue_html(SAMPLE_ISSUE_HTML)
    assert len(items) == 2

    assert items[0].section == "Big Tech & Startups"
    assert items[0].title == "Rocket Launch News"
    assert items[0].url == "https://example.com/story1?id=42"
    assert items[0].summary == "A concise summary of the rocket launch."

    assert items[1].section == "Programming, Design & Data Science"
    assert items[1].title == "Clean Engineering"
    assert items[1].url == "https://blog.example.org/post"
    assert items[1].summary == "Summary about software engineering craft."


def test_epub_renders_tldr_summary_box_as_valid_xml(tmp_path: Path):
    digest = Digest(
        source_code="TLDR",
        source_title="TLDR Tech",
        digest_date=date(2026, 11, 26),
        articles=[
            Article(
                title="Rocket Launch News",
                url="https://example.com/story1",
                body_xhtml="<p>Full extracted article body.</p>",
                source_name="TLDR Tech",
                section="Big Tech & Startups",
                domain="example.com",
                word_count=500,
                summary="Concise TLDR summary & overview.",
            )
        ],
    )
    out = tmp_path / digest.filename
    build_epub(digest, out)

    with zipfile.ZipFile(out, "r") as zf:
        chapter = zf.read("OEBPS/article_01.xhtml").decode("utf-8")
        assert "TLDR Summary" in chapter
        assert "Concise TLDR summary &amp; overview." in chapter
        assert "Big Tech &amp; Startups" in chapter
        for name in zf.namelist():
            if name.endswith((".xml", ".xhtml", ".opf", ".ncx")):
                ET.fromstring(zf.read(name))

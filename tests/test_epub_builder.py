from __future__ import annotations

from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET
import zipfile

from kobo_digest.epub_builder import build_epub
from kobo_digest.fetcher_hn import sanitize_fragment_to_xhtml
from kobo_digest.models import Article, Digest


def test_sanitize_fragment_produces_strict_xhtml():
    dirty = (
        '<div><h2>Hello & Welcome</h2>'
        '<p>Line 1<br>Line 2 <script>alert(1)</script><img src="http://example.com/a.png"></p>'
        '<a href="https://example.com/post" onclick="evil()" style="color:red">Read more</a>'
        '<a href="javascript:alert(1)">Bad link</a>'
        '<table><tr><td colspan="2" class="x">Cell</td></tr></table></div>'
    )
    xhtml, words = sanitize_fragment_to_xhtml(dirty)
    assert words >= 6
    assert "<script" not in xhtml
    assert "<img" not in xhtml
    assert "onclick" not in xhtml
    assert "javascript:" not in xhtml
    assert 'href="https://example.com/post"' in xhtml
    assert 'colspan="2"' in xhtml

    # Must parse strictly as valid XML (required by Kobo Nickel & KOReader).
    ET.fromstring(f"<root>{xhtml}</root>")


def test_build_epub_valid_structure_and_xml(tmp_path: Path):
    digest = Digest(
        source_code="HN",
        source_title="Hacker News",
        digest_date=date(2026, 11, 26),
        articles=[
            Article(
                title="Testing & Validating <EPUB> Output",
                url="https://example.com/1",
                body_xhtml="<p>First article paragraph with <strong>bold</strong> text.</p>",
                source_name="Hacker News",
                author="alice",
                domain="example.com",
                score=142,
                comment_count=38,
                comments_url="https://news.ycombinator.com/item?id=1",
                word_count=440,
            ),
            Article(
                title="Second Story",
                url="https://example.com/2",
                body_xhtml="<p>Second story body.</p><pre><code>print('hi')</code></pre>",
                source_name="Hacker News",
                author="bob",
                domain="example.org",
                score=88,
                comment_count=12,
                comments_url="https://news.ycombinator.com/item?id=2",
                word_count=220,
            ),
        ],
    )

    out_file = tmp_path / digest.filename
    build_epub(digest, out_file)
    assert out_file.exists()

    with zipfile.ZipFile(out_file, "r") as zf:
        infos = zf.infolist()
        # Spec: mimetype must be the first entry and stored uncompressed.
        assert infos[0].filename == "mimetype"
        assert infos[0].compress_type == zipfile.ZIP_STORED
        assert zf.read("mimetype") == b"application/epub+zip"

        names = set(zf.namelist())
        expected = {
            "mimetype",
            "META-INF/container.xml",
            "OEBPS/style.css",
            "OEBPS/content.opf",
            "OEBPS/toc.ncx",
            "OEBPS/nav.xhtml",
            "OEBPS/overview.xhtml",
            "OEBPS/article_01.xhtml",
            "OEBPS/article_02.xhtml",
        }
        assert expected.issubset(names)

        # Every XML / XHTML / OPF / NCX member must be strictly well-formed XML.
        for name in names:
            if name.endswith((".xml", ".xhtml", ".opf", ".ncx")):
                ET.fromstring(zf.read(name))

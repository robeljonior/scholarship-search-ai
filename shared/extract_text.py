"""HTML/PDF -> normalised text. Never executes anything from the page."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser


class _TextParser(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "td", "th"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.links: list[tuple[str, str]] = []
        self._href = None
        self._link_text: list[str] = []
        self.title = ""
        self._in_title = False
        self.h1 = ""
        self._in_h1 = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._link_text = []
        if tag == "title":
            self._in_title = True
        if tag == "h1":
            self._in_h1 = True

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join(self._link_text).strip()))
            self._href = None
        if tag == "title":
            self._in_title = False
        if tag == "h1":
            self._in_h1 = False

    def handle_data(self, data):
        if self.skip:
            return
        self.parts.append(data)
        if self._href is not None:
            self._link_text.append(data.strip())
        if self._in_title:
            self.title += data
        if self._in_h1:
            self.h1 += data


def parse_html(html: str) -> _TextParser:
    p = _TextParser()
    p.feed(html)
    p.close()
    return p


def normalise(text: str) -> str:
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def pdf_to_text(body: bytes) -> str:
    if not shutil.which("pdftotext"):
        return ""
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(body)
        f.flush()
        out = subprocess.run(["pdftotext", "-layout", f.name, "-"], capture_output=True, timeout=60)
    return out.stdout.decode("utf-8", "replace")


def extract_text(body: bytes, content_type: str) -> tuple[str, str]:
    """Returns (normalised text, html or '')."""
    if content_type == "application/pdf" or body[:5] == b"%PDF-":
        return normalise(pdf_to_text(body)), ""
    html = body.decode("utf-8", "replace")
    if content_type == "text/plain":
        return normalise(html), ""
    return normalise("".join(parse_html(html).parts)), html

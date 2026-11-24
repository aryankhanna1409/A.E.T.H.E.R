"""Web research without any API keys: DuckDuckGo search via the `ddgs`
package and direct page fetching with httpx. Network use can be switched off
in settings; everything fetched is treated as untrusted data."""
from __future__ import annotations

import re
from html.parser import HTMLParser

from .registry import Tool

MAX_PAGE_CHARS = 12_000


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        self._in_title = tag == "title"

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag in {"p", "div", "li", "h1", "h2", "h3", "br", "tr"}:
            self.parts.append("\n")
        self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    p = _TextExtractor()
    p.feed(html)
    text = re.sub(r"[ \t]+", " ", "".join(p.parts))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return p.title.strip(), text


def web_search(query: str, max_results: str = "6") -> str:
    try:
        from ddgs import DDGS
    except ImportError:
        return "Web search needs: pip install ddgs"
    results = DDGS().text(query, max_results=int(max_results))
    if not results:
        return "No results."
    return "\n\n".join(f"[{i}] {r.get('title')}\nURL: {r.get('href')}\n{r.get('body')}"
                       for i, r in enumerate(results, 1))


def web_fetch(url: str) -> str:
    import httpx
    if not re.match(r"^https?://", url):
        raise ValueError("Only http(s) URLs can be fetched.")
    r = httpx.get(url, follow_redirects=True, timeout=20,
                  headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AETHER/0.1"})
    r.raise_for_status()
    if "html" in r.headers.get("content-type", ""):
        title, text = html_to_text(r.text)
    else:
        title, text = "", r.text
    text = text[:MAX_PAGE_CHARS]
    return f"Title: {title}\nURL: {r.url}\n\n{text}"


def web_tools() -> list[Tool]:
    return [
        Tool("web_search", "search the web (DuckDuckGo)", {"query": "search terms", "max_results": "number, default 6"},
             "web.search", web_search),
        Tool("web_fetch", "fetch and read a web page", {"url": "http(s) URL"}, "web.fetch", web_fetch),
    ]

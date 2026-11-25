"""Live information from the internet, without accounts or API keys:

- weather from Open-Meteo (open data, no key), with place lookup by name,
- news headlines from RSS feeds you choose, or Google News for a topic.

Only these small requests leave the PC; your question, memories and files do
not. It all goes through the policy engine as web.fetch, so turning network
off in settings switches it off, and headlines are treated as untrusted data.
"""
from __future__ import annotations

import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import quote_plus

from ..core.logging import log

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AETHER/0.1"}
WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy", 51: "light drizzle",
       53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain",
       67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers",
       81: "showers", 82: "violent showers", 85: "snow showers", 86: "snow showers", 95: "thunderstorms",
       96: "thunderstorms with hail", 99: "thunderstorms with hail"}


def _get(url: str, timeout: float = 8.0):
    import httpx
    r = httpx.get(url, timeout=timeout, follow_redirects=True, headers=UA)
    r.raise_for_status()
    return r


class _Cache:
    def __init__(self, ttl: float):
        self.ttl, self.data, self.lock = ttl, {}, threading.Lock()

    def get(self, key, make):
        with self.lock:
            hit = self.data.get(key)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
        value = make()
        with self.lock:
            self.data[key] = (time.time(), value)
        return value


@dataclass
class Day:
    day: date
    summary: str
    high: float
    low: float
    rain_chance: int


@dataclass
class Weather:
    place: str
    now_temp: float
    now_summary: str
    days: list[Day]

    def line(self, which: int = 0, spoken: bool = False) -> str:
        d = self.days[which] if which < len(self.days) else None
        rain = f", {d.rain_chance}% chance of rain" if d and d.rain_chance >= 30 else ""
        if spoken:
            if which == 0:
                return (f"In {self.place} it's {self.now_temp:.0f} degrees and {self.now_summary}"
                        + (f", heading for {d.high:.0f} today{rain}." if d else "."))
            return f"Tomorrow in {self.place}: {d.summary}, {d.low:.0f} to {d.high:.0f} degrees{rain}." if d else ""
        if which == 0:
            return (f"{self.place}: {self.now_temp:.0f}°C, {self.now_summary}"
                    + (f"; today {d.low:.0f}-{d.high:.0f}°C, {d.summary}{rain}" if d else ""))
        return f"{self.place} {d.day:%A}: {d.summary}, {d.low:.0f}-{d.high:.0f}°C{rain}" if d else ""


@dataclass
class Headline:
    title: str
    source: str
    link: str
    published: datetime | None = None


class Online:
    def __init__(self, cfg, allowed=lambda: True):
        self.cfg = cfg  # OnlineConfig
        self.allowed = allowed  # network switch from settings
        self._geo = _Cache(7 * 86400)
        self._weather = _Cache(30 * 60)
        self._news = _Cache(30 * 60)
        self.get = _get  # swappable in tests

    # ------------------------------------------------------------ weather
    def place(self, name: str) -> tuple[str, float, float]:
        name = (name or self.cfg.location or "").strip()
        m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", name)
        if m:
            return name, float(m.group(1)), float(m.group(2))
        if not name:
            raise ValueError("I don't know where you are. Set online.location (e.g. \"Vellore\") in settings.")

        def look():
            r = self.get(f"https://geocoding-api.open-meteo.com/v1/search?name={quote_plus(name.split(',')[0])}&count=5")
            res = r.json().get("results") or []
            if not res:
                raise ValueError(f"I couldn't find a place called '{name}'.")
            hint = name.split(",")[1].strip().lower() if "," in name else ""
            best = next((x for x in res if hint and hint in f"{x.get('country', '')} {x.get('admin1', '')}".lower()), res[0])
            return best["name"], float(best["latitude"]), float(best["longitude"])
        return self._geo.get(name.lower(), look)

    def weather(self, place: str = "") -> Weather:
        if not self.allowed():
            raise PermissionError("Network access is turned off in settings.")
        label, lat, lon = self.place(place)

        def fetch():
            r = self.get("https://api.open-meteo.com/v1/forecast?latitude={:.4f}&longitude={:.4f}&timezone=auto"
                         "&forecast_days=3&current=temperature_2m,weather_code"
                         "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
                         .format(lat, lon))
            j = r.json()
            d = j["daily"]
            days = [Day(date.fromisoformat(d["time"][i]), WMO.get(d["weather_code"][i], "mixed"),
                        d["temperature_2m_max"][i], d["temperature_2m_min"][i],
                        int(d["precipitation_probability_max"][i] or 0)) for i in range(len(d["time"]))]
            cur = j.get("current", {})
            return Weather(label, cur.get("temperature_2m", days[0].high if days else 0),
                           WMO.get(cur.get("weather_code"), "mixed"), days)
        return self._weather.get((round(lat, 2), round(lon, 2)), fetch)

    # ------------------------------------------------------------ news
    def headlines(self, topic: str = "", limit: int = 5) -> list[Headline]:
        if not self.allowed():
            raise PermissionError("Network access is turned off in settings.")
        if topic.strip():
            feeds = [f"https://news.google.com/rss/search?q={quote_plus(topic.strip())}&hl=en-{self.cfg.news_region}"
                     f"&gl={self.cfg.news_region}&ceid={self.cfg.news_region}:en"]
        else:
            feeds = list(self.cfg.news_feeds)
        out: list[Headline] = []
        for url in feeds:
            try:
                out += self._news.get(url, lambda u=url: parse_feed(self.get(u).text))
            except Exception as e:
                log.warning("news feed %s failed: %s", url, e)
        seen, uniq = set(), []
        for h in sorted(out, key=lambda h: h.published or datetime.min, reverse=True):
            key = re.sub(r"\W+", " ", h.title.lower()).strip()[:60]
            if key not in seen:
                seen.add(key)
                uniq.append(h)
        return uniq[:limit]


def parse_feed(xml: str) -> list[Headline]:
    """RSS 2.0 and Atom. Titles are trimmed of ' - Source' suffixes that Google News adds."""
    if len(xml) > 3_000_000:
        raise ValueError("feed too large")
    root = ET.fromstring(xml)
    out = []
    atom = "{http://www.w3.org/2005/Atom}"
    channel = root.find("channel")
    src_default = (channel.findtext("title") if channel is not None else root.findtext(f"{atom}title")) or ""
    for it in root.iter():
        tag = it.tag
        if tag not in ("item", f"{atom}entry"):
            continue
        title = unescape((it.findtext("title") or it.findtext(f"{atom}title") or "").strip())
        if not title:
            continue
        source = (it.findtext("source") or "").strip()
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        elif not source and " - " in title and len(title.rsplit(" - ", 1)[1]) <= 40:
            title, source = title.rsplit(" - ", 1)
        link_el = it.find(f"{atom}link")
        link = (it.findtext("link") or (link_el.get("href") if link_el is not None else "") or "").strip()
        when = None
        raw = it.findtext("pubDate") or it.findtext(f"{atom}updated") or it.findtext(f"{atom}published")
        if raw:
            try:
                when = parsedate_to_datetime(raw) if "," in raw else datetime.fromisoformat(raw.replace("Z", "+00:00"))
                when = when.astimezone().replace(tzinfo=None) if when.tzinfo else when
            except (TypeError, ValueError):
                when = None
        out.append(Headline(re.sub(r"\s+", " ", title)[:200], (source or src_default)[:60], link, when))
    return out

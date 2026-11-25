"""Local mail: read email that is already on this PC, and write reply drafts that
you send yourself. Nothing is fetched from a server and nothing is ever sent.

Sources:
- .eml files and .mbox archives in folders you choose (Outlook: drag messages
  to a folder; Thunderbird: the profile's mbox files; Gmail: Google Takeout),
- optionally the classic Outlook desktop app on Windows through COM (read-only).

Drafts are written as .eml with `X-Unsent: 1`, which Outlook and Windows Mail
open as an editable, unsent message: AETHER prepares, you press Send.
"""
from __future__ import annotations

import email
import email.policy
import html
import mailbox
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path

from ..core.logging import log


@dataclass
class Mail:
    id: str
    sender: str  # display name, or the address
    address: str
    subject: str
    date: datetime  # naive local time
    snippet: str
    to: str = ""
    source: str = ""

    def as_dict(self) -> dict:
        return {"id": self.id, "sender": self.sender, "address": self.address, "subject": self.subject,
                "date": self.date.isoformat(timespec="minutes"), "snippet": self.snippet, "to": self.to,
                "source": self.source}

    def line(self) -> str:
        return f"{self.date:%a %d %b %H:%M} · {self.sender}: {self.subject or '(no subject)'}"


def _text(msg) -> str:
    """Plain-text body; falls back to the HTML part with tags stripped."""
    try:
        part = msg.get_body(preferencelist=("plain", "html")) if hasattr(msg, "get_body") else None
    except Exception:
        part = None
    if part is None:
        part = msg
    try:
        body = part.get_content() if hasattr(part, "get_content") else part.get_payload(decode=True) or ""
    except Exception:
        body = part.get_payload(decode=True) or b""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    if not isinstance(body, str):
        return ""
    if part.get_content_type() == "text/html":
        body = re.sub(r"(?is)<(script|style).*?</\1>", " ", body)
        body = html.unescape(re.sub(r"<[^>]+>", " ", body))
    lines = [l for l in body.splitlines() if not l.lstrip().startswith(">")]  # drop quoted history
    cut = []
    for l in lines:
        if re.match(r"^\s*(On .+ wrote:|-----Original Message-----|From: .+)$", l):
            break
        cut.append(l)
    return re.sub(r"\s+", " ", " ".join(cut)).strip()


def _local(d: datetime | None) -> datetime:
    if d is None:
        return datetime.fromtimestamp(0)
    return d.astimezone().replace(tzinfo=None) if d.tzinfo else d


def from_message(msg, source: str = "") -> Mail:
    name, addr = parseaddr(str(msg.get("From", "")))
    try:
        when = parsedate_to_datetime(str(msg.get("Date"))) if msg.get("Date") else None
    except (TypeError, ValueError):
        when = None
    to = ", ".join(a for _, a in getaddresses([str(msg.get("To", ""))]) if a)
    return Mail(str(msg.get("Message-ID", "")).strip() or f"{addr}:{msg.get('Date')}:{msg.get('Subject')}",
                name or addr or "(unknown)", addr.lower(), re.sub(r"\s+", " ", str(msg.get("Subject", ""))).strip(),
                _local(when), _text(msg)[:600], to, source)


class Mailbox:
    def __init__(self, paths: list[str], outlook: bool = False, drafts: str = ""):
        self.paths = [Path(p).expanduser() for p in paths]
        self.outlook = outlook
        self.drafts = Path(drafts).expanduser() if drafts else (self.paths[0] / "drafts" if self.paths else Path("drafts"))
        self._cache: dict[Path, tuple[float, list[Mail]]] = {}
        self._lock = threading.Lock()

    def files(self) -> list[Path]:
        out = []
        for p in self.paths:
            if p.is_file():
                out.append(p)
            elif p.is_dir():
                out += sorted(x for x in p.rglob("*") if x.is_file() and self.drafts not in x.parents
                              and (x.suffix.lower() in {".eml", ".mbox"} or (not x.suffix and _looks_mbox(x))))
        return out

    def _load(self, f: Path) -> list[Mail]:
        mtime = f.stat().st_mtime
        with self._lock:
            hit = self._cache.get(f)
            if hit and hit[0] == mtime:
                return hit[1]
        try:
            if f.suffix.lower() == ".eml":
                msgs = [from_message(email.message_from_bytes(f.read_bytes(), policy=email.policy.default), f.name)]
            else:
                box = mailbox.mbox(str(f), factory=lambda fp: email.message_from_binary_file(fp, policy=email.policy.default),
                                   create=False)
                msgs = [from_message(m, f.name) for m in box]
                box.close()
        except Exception as e:  # a broken file must not break the brief
            log.warning("mail %s unreadable: %s", f, e)
            msgs = []
        with self._lock:
            self._cache[f] = (mtime, msgs)
        return msgs

    def all(self, since: datetime | None = None) -> list[Mail]:
        out: list[Mail] = []
        for f in self.files():
            out += self._load(f)
        if self.outlook:
            out += self._outlook(since or datetime.now() - timedelta(days=14))
        seen, uniq = set(), []
        for m in sorted(out, key=lambda m: m.date, reverse=True):
            if m.id not in seen and (since is None or m.date >= since):
                seen.add(m.id)
                uniq.append(m)
        return uniq

    def recent(self, days: float = 2, limit: int = 20, now: datetime | None = None) -> list[Mail]:
        return self.all((now or datetime.now()) - timedelta(days=days))[:limit]

    def search(self, query: str, limit: int = 10) -> list[Mail]:
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 1]
        if not words:
            return self.all()[:limit]
        scored = []
        for m in self.all():
            hay = f"{m.sender} {m.address} {m.subject} {m.snippet}".lower()
            score = sum(3 if w in f"{m.sender} {m.address} {m.subject}".lower() else 1 for w in words if w in hay)
            if score:
                scored.append((score, m.date, m))
        return [m for _, _, m in sorted(scored, key=lambda x: (x[0], x[1]), reverse=True)[:limit]]

    def find(self, mid: str) -> Mail | None:
        return next((m for m in self.all() if m.id == mid), None)

    def draft(self, to: str, subject: str, body: str, reply_to: Mail | None = None) -> Path:
        """Writes an unsent .eml that Outlook / Windows Mail opens ready to edit and send. Never sends."""
        msg = EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject
        msg["X-Unsent"] = "1"
        if reply_to and reply_to.id.startswith("<"):
            msg["In-Reply-To"] = reply_to.id
            msg["References"] = reply_to.id
        msg.set_content(body)
        self.drafts.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^\w-]+", "-", subject.lower()).strip("-")[:40] or "draft"
        path = self.drafts / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}.eml"
        path.write_bytes(bytes(msg))
        return path

    def _outlook(self, since: datetime) -> list[Mail]:  # pragma: no cover - Windows + Outlook only
        """Classic Outlook through COM (pywin32): the default inbox, read-only, from the local store."""
        try:
            import win32com.client
            items = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI").GetDefaultFolder(6).Items
            items.Sort("[ReceivedTime]", True)
            found = items.Restrict(f"[ReceivedTime] >= '{since.strftime('%m/%d/%Y %I:%M %p')}'")
            out = []
            for it in found:
                if getattr(it, "Class", 43) != 43:  # mail items only
                    continue
                when = datetime.fromtimestamp(it.ReceivedTime.timestamp())
                body = re.sub(r"\s+", " ", str(it.Body or ""))[:600]
                out.append(Mail(str(it.EntryID), str(it.SenderName or ""), str(it.SenderEmailAddress or "").lower(),
                                str(it.Subject or ""), when, body, str(it.To or ""), "Outlook"))
                if len(out) >= 300:
                    break
            return out
        except Exception as e:
            log.warning("Outlook mail unavailable: %s", e)
            return []


def _looks_mbox(p: Path) -> bool:
    """Thunderbird keeps folders as extension-less mbox files ('Inbox')."""
    if p.stat().st_size < 20:
        return False
    try:
        with p.open("rb") as f:
            return f.read(5) == b"From "
    except OSError:
        return False


def describe(mails: list[Mail], label: str = "", vip: set[str] | None = None) -> str:
    if not mails:
        return f"No email {label}.".replace("  ", " ").strip()
    vip = vip or set()
    lines = [f"{len(mails)} email{'s' if len(mails) != 1 else ''}{(' ' + label) if label else ''}:"]
    for m in mails:
        star = "★ " if is_vip(m, vip) else ""
        lines.append(f"- {star}{m.line()}" + (f"\n  {m.snippet[:180]}" if m.snippet else ""))
    return "\n".join(lines)


def is_vip(m: Mail, vip: set[str]) -> bool:
    low = f"{m.sender} {m.address}".lower()
    return any(v and v.lower() in low for v in vip)

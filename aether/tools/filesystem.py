"""Filesystem tools. Paths are checked by the policy engine (allowed roots,
secrets denylist) before these functions ever run."""
from __future__ import annotations

import os
from pathlib import Path

from .registry import Tool

TEXT_EXT = {".txt", ".md", ".py", ".json", ".csv", ".toml", ".yaml", ".yml", ".html", ".js", ".ts", ".tsx",
            ".java", ".c", ".cpp", ".h", ".rs", ".go", ".ini", ".log", ".tex", ".rst"}
MAX_CHARS = 20_000


def read_text_any(path: Path) -> str:
    """Read txt/md/code directly; PDF and DOCX when the optional extras are installed."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return "[PDF support needs: pip install pypdf]"
        return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
    if suffix == ".docx":
        try:
            import docx
        except ImportError:
            return "[DOCX support needs: pip install python-docx]"
        return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
    return path.read_text(encoding="utf-8", errors="replace")


def fs_read(path: str) -> str:
    p = Path(path).expanduser().resolve()
    text = read_text_any(p)
    return text[:MAX_CHARS] + (f"\n...[truncated, {len(text)} chars total]" if len(text) > MAX_CHARS else "")


def fs_list(path: str) -> str:
    p = Path(path).expanduser().resolve()
    entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:300]
    return "\n".join(f"{'[dir] ' if e.is_dir() else ''}{e.name}" for e in entries) or "(empty)"


def fs_search(path: str, query: str) -> str:
    root = Path(path).expanduser().resolve()
    q = query.lower()
    hits = []
    for dirpath, _, files in os.walk(root):
        for f in files:
            fp = Path(dirpath) / f
            if q in f.lower():
                hits.append(f"name match: {fp}")
            elif fp.suffix.lower() in TEXT_EXT and fp.stat().st_size < 2_000_000:
                try:
                    for i, line in enumerate(fp.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                        if q in line.lower():
                            hits.append(f"{fp}:{i}: {line.strip()[:160]}")
                            break
                except OSError:
                    continue
            if len(hits) >= 50:
                return "\n".join(hits) + "\n...(more results truncated)"
    return "\n".join(hits) or "No matches."


def fs_create(path: str, content: str) -> str:
    p = Path(path).expanduser().resolve()
    if p.exists():
        raise FileExistsError(f"{p} already exists; use fs_modify to change it.")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    # Verify instead of assuming success.
    if p.read_text(encoding="utf-8") != content:
        raise IOError("File was written but its content does not match.")
    return f"Created {p} ({len(content)} chars, verified)."


def fs_modify(path: str, content: str) -> str:
    p = Path(path).expanduser().resolve()
    backup = p.with_suffix(p.suffix + ".aether-bak")
    if p.exists():
        backup.write_bytes(p.read_bytes())
    p.write_text(content, encoding="utf-8")
    if p.read_text(encoding="utf-8") != content:
        raise IOError("File was written but its content does not match.")
    return f"Updated {p} (backup at {backup.name}, verified)."


def filesystem_tools(journal=None) -> list[Tool]:
    def create(path: str, content: str) -> str:
        out = fs_create(path, content)
        if journal:
            journal.log("create", str(Path(path).expanduser().resolve()))
        return out

    def modify(path: str, content: str) -> str:
        p = Path(path).expanduser().resolve()
        existed = p.exists()
        out = fs_modify(path, content)
        if journal:
            if existed:
                journal.log("modify", str(p), backup=str(p.with_suffix(p.suffix + ".aether-bak")))
            else:
                journal.log("create", str(p))
        return out

    return [
        Tool("fs_read", "read a text, code, PDF or DOCX file", {"path": "file path"}, "fs.read", fs_read, "path"),
        Tool("fs_list", "list a folder", {"path": "folder path"}, "fs.list", fs_list, "path"),
        Tool("fs_search", "search file names and contents in a folder", {"path": "folder", "query": "text"},
             "fs.search", fs_search, "path"),
        Tool("fs_create", "create a new file (asks first)", {"path": "file path", "content": "text"},
             "fs.create", create, "path"),
        Tool("fs_modify", "overwrite a file, keeping a backup (asks first)", {"path": "file path", "content": "text"},
             "fs.modify", modify, "path"),
    ]

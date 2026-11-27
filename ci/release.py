"""Release helper for the Jenkins pipeline.

AETHER ships in "Marks": the major version in pyproject.toml is the Mark number
(29.0.0 is Mark XXIX, 30.0.0 is Mark XXX). On every green build of main the
pipeline calls `publish`, which makes sure a GitHub release named after the Mark
exists, tagged v<version> at the built commit, with the Windows installer
attached when the build produced one.

It is idempotent: if the Mark is already released, nothing changes, so only a
version bump creates a new release. Standard library only, so it runs on any
agent with Python 3.11+.

    python ci/release.py info
    python ci/release.py publish --repo owner/name --commit <sha> [--asset dist/AETHER-Setup-30.0.0.exe] [--dry-run]

`publish` reads the token from the GITHUB_TOKEN environment variable.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"

_NUMERALS = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
             (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]


def roman(n: int) -> str:
    if n < 1:
        raise ValueError(f"Mark numbers start at 1, got {n}")
    out = []
    for value, numeral in _NUMERALS:
        count, n = divmod(n, value)
        out.append(numeral * count)
    return "".join(out)


def read_version(pyproject: Path = ROOT / "pyproject.toml") -> str:
    with pyproject.open("rb") as f:
        return tomllib.load(f)["project"]["version"]


@dataclass(frozen=True)
class Mark:
    version: str

    @property
    def number(self) -> int:
        return int(self.version.split(".")[0])

    @property
    def name(self) -> str:
        return f"Mark {roman(self.number)}"

    @property
    def tag(self) -> str:
        return f"v{self.version}"


# --- GitHub ---------------------------------------------------------------------------------------

Http = Callable[[str, str, dict, bytes | None], tuple[int, dict]]


def _urllib_http(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return e.code, {"message": raw.decode(errors="replace")}


class GitHub:
    def __init__(self, repo: str, token: str, http: Http = _urllib_http):
        self.repo, self.token, self.http = repo, token, http

    def _call(self, method: str, url: str, body: bytes | None = None,
              content_type: str = "application/json") -> tuple[int, dict]:
        if not url.startswith("http"):
            url = f"{API}/repos/{self.repo}{url}"
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "aether-ci"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            headers["Content-Type"] = content_type
        return self.http(method, url, headers, body)

    def _ok(self, status: int, data: dict, what: str) -> dict:
        if status >= 300:
            raise RuntimeError(f"GitHub {what} failed ({status}): {data.get('message', data)}")
        return data

    def release_for(self, tag: str) -> dict | None:
        status, data = self._call("GET", f"/releases/tags/{urllib.parse.quote(tag)}")
        return None if status == 404 else self._ok(status, data, f"lookup of release {tag}")

    def tag_commit(self, tag: str) -> str | None:
        status, data = self._call("GET", f"/git/ref/tags/{urllib.parse.quote(tag)}")
        if status == 404:
            return None
        obj = self._ok(status, data, f"lookup of tag {tag}")["object"]
        if obj["type"] == "tag":  # annotated tag: follow it to the commit
            obj = self._ok(*self._call("GET", obj["url"]), f"lookup of tag object {tag}")["object"]
        return obj["sha"]

    def create_release(self, mark: Mark, commit: str, body: str) -> dict:
        payload = {"tag_name": mark.tag, "target_commitish": commit, "name": mark.name, "body": body,
                   "generate_release_notes": True, "draft": False, "prerelease": False}
        return self._ok(*self._call("POST", "/releases", json.dumps(payload).encode()), "release creation")

    def upload_asset(self, release: dict, path: Path) -> dict:
        base = release["upload_url"].split("{", 1)[0]
        url = f"{base}?name={urllib.parse.quote(path.name)}"
        return self._ok(*self._call("POST", url, path.read_bytes(), "application/octet-stream"),
                        f"upload of {path.name}")


def publish(gh: GitHub, mark: Mark, commit: str, assets: list[Path], build_url: str = "",
            dry_run: bool = False, log: Callable[[str], None] = print) -> str:
    """Create the Mark's release if needed and attach missing assets. Returns what happened."""
    release = gh.release_for(mark.tag)
    if release is None:
        if dry_run:
            log(f"[dry run] would create release '{mark.name}' at {mark.tag} -> {commit[:12]}")
            return "would-create"
        body = f"AETHER {mark.name} (`{mark.version}`)."
        if build_url:
            body += f" Built and tested by Jenkins: {build_url}"
        release = gh.create_release(mark, commit, body)
        log(f"Created release '{mark.name}' at {mark.tag}: {release.get('html_url', '')}")
        outcome = "created"
    else:
        tagged = gh.tag_commit(mark.tag)
        if tagged and tagged != commit:
            log(f"{mark.name} is already released at {mark.tag} ({tagged[:12]}); bump the version in "
                "pyproject.toml to cut the next Mark. Nothing to do.")
            return "exists"
        outcome = "updated"

    have = {a["name"] for a in release.get("assets", [])}
    for path in assets:
        if path.name in have:
            log(f"{path.name} is already attached")
        elif dry_run:
            log(f"[dry run] would upload {path.name}")
        else:
            gh.upload_asset(release, path)
            log(f"Uploaded {path.name}")
    return outcome


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="print MARK_NAME, MARK_TAG and MARK_VERSION as KEY=value lines")
    pub = sub.add_parser("publish", help="create the GitHub release for the current Mark if needed")
    pub.add_argument("--repo", required=True, help="owner/name")
    pub.add_argument("--commit", required=True)
    pub.add_argument("--asset", action="append", default=[], type=Path)
    pub.add_argument("--build-url", default=os.environ.get("BUILD_URL", ""))
    pub.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    mark = Mark(read_version())
    if args.cmd == "info":
        print(f"MARK_VERSION={mark.version}\nMARK_NAME={mark.name}\nMARK_TAG={mark.tag}")
        return 0

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token and not args.dry_run:
        print("GITHUB_TOKEN is not set", file=sys.stderr)
        return 2
    missing = [a for a in args.asset if not a.is_file()]
    if missing:
        print(f"Asset not found: {', '.join(map(str, missing))}", file=sys.stderr)
        return 2
    publish(GitHub(args.repo, token), mark, args.commit, args.asset, args.build_url, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())

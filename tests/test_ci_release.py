"""The Jenkins release helper: Mark naming and idempotent GitHub releases (no network)."""
import json

import pytest

from ci.release import GitHub, Mark, main, publish, read_version, roman


def test_roman_numerals():
    assert [roman(n) for n in (1, 4, 9, 14, 29, 30, 40, 49, 99)] == \
        ["I", "IV", "IX", "XIV", "XXIX", "XXX", "XL", "XLIX", "XCIX"]
    with pytest.raises(ValueError):
        roman(0)


def test_mark_from_version():
    m = Mark("30.0.0")
    assert (m.number, m.name, m.tag) == (30, "Mark XXX", "v30.0.0")
    assert Mark("31.2.0").name == "Mark XXXI"


def test_current_version_is_a_mark():
    assert Mark(read_version()).name.startswith("Mark ")


def test_info_command(capsys):
    assert main(["info"]) == 0
    out = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert out["MARK_TAG"] == f"v{out['MARK_VERSION']}"


class FakeGitHub:
    """Records calls and answers like the GitHub REST API."""

    def __init__(self, release=None, tag_sha=None):
        self.release, self.tag_sha, self.calls = release, tag_sha, []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body))
        assert headers["Authorization"] == "Bearer t0k"
        if method == "GET" and "/releases/tags/" in url:
            return (200, self.release) if self.release else (404, {"message": "Not Found"})
        if method == "GET" and "/git/ref/tags/" in url:
            return (200, {"object": {"type": "commit", "sha": self.tag_sha}}) if self.tag_sha else (404, {})
        if method == "POST" and url.endswith("/releases"):
            self.release = {"id": 1, "html_url": "https://github.com/o/r/releases/1", "assets": [],
                            "upload_url": "https://uploads.github.com/repos/o/r/releases/1/assets{?name,label}"}
            return 201, self.release
        if method == "POST" and url.startswith("https://uploads.github.com/"):
            return 201, {"name": url.split("name=")[1]}
        raise AssertionError(f"unexpected call {method} {url}")


def test_publish_creates_release_and_uploads_installer(tmp_path):
    exe = tmp_path / "AETHER-Setup-30.0.0.exe"
    exe.write_bytes(b"MZ")
    fake = FakeGitHub()
    out = publish(GitHub("o/r", "t0k", fake), Mark("30.0.0"), "abc123", [exe], "https://ci/job/1/", log=lambda _: None)
    assert out == "created"
    create = next(json.loads(b) for m, u, b in fake.calls if u.endswith("/releases"))
    assert create["tag_name"] == "v30.0.0" and create["name"] == "Mark XXX"
    assert create["target_commitish"] == "abc123" and "https://ci/job/1/" in create["body"]
    upload = fake.calls[-1]
    assert upload[1] == "https://uploads.github.com/repos/o/r/releases/1/assets?name=AETHER-Setup-30.0.0.exe"
    assert upload[2] == b"MZ"


def test_publish_is_a_no_op_once_the_mark_is_released_at_another_commit(tmp_path):
    exe = tmp_path / "AETHER-Setup-30.0.0.exe"
    exe.write_bytes(b"MZ")
    fake = FakeGitHub(release={"id": 1, "assets": [], "upload_url": "u{?name}"}, tag_sha="old999")
    out = publish(GitHub("o/r", "t0k", fake), Mark("30.0.0"), "new111", [exe], log=lambda _: None)
    assert out == "exists"
    assert all(m == "GET" for m, _, _ in fake.calls)


def test_publish_retry_on_same_commit_only_uploads_missing_assets(tmp_path):
    exe = tmp_path / "AETHER-Setup-30.0.0.exe"
    exe.write_bytes(b"MZ")
    release = {"id": 1, "assets": [{"name": exe.name}],
               "upload_url": "https://uploads.github.com/repos/o/r/releases/1/assets{?name,label}"}
    fake = FakeGitHub(release=release, tag_sha="abc123")
    assert publish(GitHub("o/r", "t0k", fake), Mark("30.0.0"), "abc123", [exe], log=lambda _: None) == "updated"
    assert not [c for c in fake.calls if c[0] == "POST"]


def test_dry_run_changes_nothing():
    fake = FakeGitHub()
    assert publish(GitHub("o/r", "t0k", fake), Mark("30.0.0"), "abc123", [], dry_run=True,
                   log=lambda _: None) == "would-create"
    assert [m for m, _, _ in fake.calls] == ["GET"]

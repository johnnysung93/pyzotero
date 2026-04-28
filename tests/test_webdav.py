"""Tests for Zotero WebDAV attachment storage helpers."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import httpx

from pyzotero._config import update_env_file
from pyzotero._webdav import (
    WebDAVConfig,
    WebDAVStorage,
    attachment_template_for_file,
    build_prop,
    parse_prop,
)


def test_prop_roundtrip():
    prop = build_prop(1710000000123, "abc123")
    assert parse_prop(prop) == {"mtime": 1710000000123, "md5": "abc123"}


def test_attachment_template_for_file(tmp_path):
    file_path = tmp_path / "paper.pdf"
    file_path.write_bytes(b"pdf bytes")

    template = attachment_template_for_file(file_path)

    assert template["itemType"] == "attachment"
    assert template["linkMode"] == "imported_file"
    assert template["filename"] == "paper.pdf"
    assert template["contentType"] == "application/pdf"
    assert template["md5"] == hashlib.md5(b"pdf bytes").hexdigest()  # noqa: S324
    assert isinstance(template["mtime"], int)


def test_webdav_upload_file_sequence(tmp_path):
    file_path = tmp_path / "paper.pdf"
    file_path.write_bytes(b"pdf bytes")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204, request=request)

    storage = WebDAVStorage(
        WebDAVConfig(
            url="https://example.test/zotero/",
            username="user",
            password="pass",
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    storage.upload_file("ABC12345", file_path, filename="paper.pdf", mtime=123, md5="abc")

    assert [request.method for request in requests] == ["DELETE", "PUT", "PUT"]
    assert str(requests[0].url) == "https://example.test/zotero/ABC12345.prop"
    assert str(requests[1].url) == "https://example.test/zotero/ABC12345.zip"
    assert str(requests[2].url) == "https://example.test/zotero/ABC12345.prop"
    assert b"<mtime>123</mtime>" in requests[2].content
    assert b"<hash>abc</hash>" in requests[2].content

    zip_payload = requests[1].content
    zip_path = tmp_path / "payload.zip"
    zip_path.write_bytes(zip_payload)
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.namelist() == ["paper.pdf"]
        assert archive.read("paper.pdf") == b"pdf bytes"


def test_webdav_download_attachment(tmp_path):
    zip_path = tmp_path / "remote.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("wrong-name.pdf", b"pdf bytes")
    zip_payload = zip_path.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith(".prop"):
            return httpx.Response(200, content=build_prop(123, "abc"), request=request)
        return httpx.Response(200, content=zip_payload, request=request)

    storage = WebDAVStorage(
        WebDAVConfig(
            url="https://example.test/zotero/",
            username="user",
            password="pass",
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = storage.download_attachment("ABC12345", tmp_path / "out", filename="paper.pdf")

    assert result["metadata"] == {"mtime": 123, "md5": "abc"}
    assert result["files"] == [str(tmp_path / "out" / "paper.pdf")]
    assert Path(result["files"][0]).read_bytes() == b"pdf bytes"


def test_update_env_file_preserves_unrelated_values(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("ZOTERO_API_KEY=secret\nWEBDAV_URL=old\n# keep me\n", encoding="utf-8")

    update_env_file(
        {
            "WEBDAV_URL": "https://example.test/webdav",
            "WEBDAV_USER": "user name",
        },
        env_path,
    )

    assert env_path.read_text(encoding="utf-8").splitlines() == [
        "ZOTERO_API_KEY=secret",
        "WEBDAV_URL=https://example.test/webdav",
        "# keep me",
        "",
        "WEBDAV_USER='user name'",
    ]

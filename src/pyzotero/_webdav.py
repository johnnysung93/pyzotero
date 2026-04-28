"""Zotero-compatible WebDAV attachment storage helpers."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from xml.etree import ElementTree

import httpx

from ._config import getenv_any, load_env
from .errors import FileDoesNotExistError, MissingCredentialsError, PyZoteroError


class WebDAVError(PyZoteroError):
    """Raised when Zotero WebDAV storage cannot be read or written."""


@dataclass(frozen=True)
class WebDAVConfig:
    """Connection details for a Zotero WebDAV storage directory."""

    url: str
    username: str
    password: str
    auth: str = "basic"
    timeout: float = 60.0

    @classmethod
    def from_env(cls) -> WebDAVConfig:
        """Build config from environment, loading ``~/.config/pyzotero/.env`` first."""
        load_env()
        url = getenv_any("PYZOTERO_WEBDAV_URL", "WEBDAV_URL").strip()
        username = getenv_any("PYZOTERO_WEBDAV_USERNAME", "WEBDAV_USER", "WEBDAV_USERNAME").strip()
        password = getenv_any("PYZOTERO_WEBDAV_PASSWORD", "WEBDAV_PASS", "WEBDAV_PASSWORD")
        auth = os.getenv("PYZOTERO_WEBDAV_AUTH", "basic").strip().lower()
        timeout = float(os.getenv("PYZOTERO_WEBDAV_TIMEOUT", "60"))
        if not url or not username or not password:
            msg = (
                "Set PYZOTERO_WEBDAV_URL, PYZOTERO_WEBDAV_USERNAME, and "
                "PYZOTERO_WEBDAV_PASSWORD in ~/.config/pyzotero/.env or the environment"
            )
            raise MissingCredentialsError(msg)
        return cls(url=_zotero_root_url(url), username=username, password=password, auth=auth, timeout=timeout)


def _zotero_root_url(url: str) -> str:
    """Return the WebDAV URL for the Zotero storage root ending in ``/zotero/``."""
    clean = url.strip()
    if not clean.endswith("/"):
        clean += "/"
    if clean.rstrip("/").lower().endswith("/zotero"):
        return clean
    return urljoin(clean, "zotero/")


def file_mtime_ms(path: str | os.PathLike[str]) -> int:
    """Return a file modification time in Zotero's millisecond format."""
    return int(Path(path).stat().st_mtime * 1000)


def file_md5(path: str | os.PathLike[str]) -> str:
    """Return the MD5 hash Zotero stores for attachment content."""
    digest = hashlib.md5()  # noqa: S324 - Zotero uses MD5 for attachment sync metadata.
    with Path(path).open("rb") as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def guess_content_type(path: str | os.PathLike[str]) -> str:
    """Return a MIME type for a local attachment file."""
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def attachment_template_for_file(
    file_path: str | os.PathLike[str],
    title: str | None = None,
) -> dict[str, Any]:
    """Create Zotero attachment metadata for a stored file."""
    path = Path(file_path)
    if not path.is_file():
        raise FileDoesNotExistError(f"The file at {path!s} couldn't be opened or found.")
    return {
        "itemType": "attachment",
        "linkMode": "imported_file",
        "title": title or path.name,
        "filename": path.name,
        "contentType": guess_content_type(path),
        "mtime": file_mtime_ms(path),
        "md5": file_md5(path),
    }


class WebDAVStorage:
    """Read and write Zotero attachment payloads on a WebDAV server."""

    def __init__(
        self,
        config: WebDAVConfig | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config or WebDAVConfig.from_env()
        auth: httpx.Auth
        if self.config.auth == "digest":
            auth = httpx.DigestAuth(self.config.username, self.config.password)
        else:
            auth = httpx.BasicAuth(self.config.username, self.config.password)
        self.client = client or httpx.Client(auth=auth, timeout=self.config.timeout)

    def verify(self) -> bool:
        """Verify that the Zotero WebDAV directory exists and is writable."""
        response = self.client.request(
            "PROPFIND",
            self.config.url,
            headers={"Depth": "0", "Content-Type": "text/xml; charset=utf-8"},
            content="<propfind xmlns='DAV:'><prop><getcontentlength/></prop></propfind>",
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            raise WebDAVError(f"Zotero WebDAV directory not found: {self.config.url}")
        self._raise_for_status(response, "PROPFIND")

        test_url = urljoin(self.config.url, "pyzotero-test-file.prop")
        put = self.client.put(test_url, content=b" ", headers={"Content-Type": "text/plain"})
        self._raise_for_status(put, "PUT")
        get = self.client.get(test_url)
        self._raise_for_status(get, "GET")
        delete = self.client.delete(test_url)
        self._raise_for_status(delete, "DELETE", allowed={200, 204, 404})
        return True

    def get_metadata(self, key: str) -> dict[str, Any] | None:
        """Read ``KEY.prop`` metadata from WebDAV."""
        response = self.client.get(self._prop_url(key))
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        self._raise_for_status(response, "GET")
        if not response.text:
            return None
        return parse_prop(response.text)

    def upload_file(
        self,
        key: str,
        file_path: str | os.PathLike[str],
        *,
        filename: str | None = None,
        mtime: int | None = None,
        md5: str | None = None,
        overwrite: bool = True,
    ) -> dict[str, Any]:
        """Upload a single file using Zotero's WebDAV ``KEY.zip``/``KEY.prop`` format."""
        path = Path(file_path)
        if not path.is_file():
            raise FileDoesNotExistError(f"The file at {path!s} couldn't be opened or found.")
        metadata = {
            "mtime": mtime if mtime is not None else file_mtime_ms(path),
            "md5": md5 or file_md5(path),
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = Path(tmpdir) / f"{key}.zip"
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(path, arcname=filename or path.name)
            self.upload_zip(key, zip_path, metadata, overwrite=overwrite)
        return metadata

    def upload_zip(
        self,
        key: str,
        zip_path: str | os.PathLike[str],
        metadata: dict[str, Any],
        *,
        overwrite: bool = True,
    ) -> None:
        """Upload an existing Zotero attachment zip and matching metadata."""
        if not overwrite and self.get_metadata(key):
            raise WebDAVError(f"Attachment {key} already exists on WebDAV")
        if overwrite:
            delete = self.client.delete(self._prop_url(key))
            self._raise_for_status(delete, "DELETE", allowed={200, 204, 404})
        with Path(zip_path).open("rb") as src:
            put_zip = self.client.put(
                self._zip_url(key),
                content=src.read(),
                headers={"Content-Type": "application/zip"},
            )
        self._raise_for_status(put_zip, "PUT")
        put_prop = self.client.put(
            self._prop_url(key),
            content=build_prop(int(metadata["mtime"]), str(metadata["md5"])),
            headers={"Content-Type": "text/xml"},
        )
        self._raise_for_status(put_prop, "PUT")

    def download_attachment(
        self,
        key: str,
        output_dir: str | os.PathLike[str],
        *,
        filename: str | None = None,
    ) -> dict[str, Any]:
        """Download and extract ``KEY.zip`` from WebDAV into ``output_dir``."""
        metadata = self.get_metadata(key)
        response = self.client.get(self._zip_url(key))
        if response.status_code == httpx.codes.NOT_FOUND:
            raise WebDAVError(f"Attachment ZIP not found on WebDAV for key {key}")
        self._raise_for_status(response, "GET")
        target_dir = Path(output_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        extracted: list[str] = []
        with tempfile.NamedTemporaryFile(suffix=".zip") as tmp:
            tmp.write(response.content)
            tmp.flush()
            with zipfile.ZipFile(tmp.name) as archive:
                members = [m for m in archive.infolist() if not m.is_dir()]
                archive.extractall(target_dir)
                extracted = [str(target_dir / m.filename) for m in members]
        if filename and len(extracted) == 1:
            current = Path(extracted[0])
            desired = target_dir / filename
            if current != desired:
                current.replace(desired)
                extracted = [str(desired)]
        return {"key": key, "metadata": metadata, "files": extracted}

    def _zip_url(self, key: str) -> str:
        return urljoin(self.config.url, f"{key.upper()}.zip")

    def _prop_url(self, key: str) -> str:
        return urljoin(self.config.url, f"{key.upper()}.prop")

    @staticmethod
    def _raise_for_status(
        response: httpx.Response,
        method: str,
        *,
        allowed: set[int] | None = None,
    ) -> None:
        allowed_statuses = allowed or {200, 201, 204, 207}
        if response.status_code in allowed_statuses:
            return
        msg = (
            f"HTTP {response.status_code} from WebDAV server for {method} "
            f"{response.request.url}: {response.text}"
        )
        raise WebDAVError(msg)


def build_prop(mtime: int, md5: str) -> str:
    """Build Zotero's WebDAV sidecar metadata document."""
    return (
        '<properties version="1">'
        f"<mtime>{mtime}</mtime>"
        f"<hash>{md5}</hash>"
        "</properties>"
    )


def parse_prop(text: str) -> dict[str, Any]:
    """Parse Zotero's WebDAV sidecar metadata document."""
    try:
        root = ElementTree.fromstring(text)
        mtime = root.findtext("mtime")
        md5 = root.findtext("hash")
    except ElementTree.ParseError:
        stripped = text.strip()
        if stripped.isdigit():
            return {"mtime": int(stripped) * 1000, "md5": None}
        raise WebDAVError("Invalid Zotero WebDAV .prop file") from None
    if not mtime or not mtime.isdigit():
        raise WebDAVError("Invalid Zotero WebDAV .prop mtime")
    return {"mtime": int(mtime), "md5": md5}

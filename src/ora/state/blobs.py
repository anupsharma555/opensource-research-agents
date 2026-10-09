"""Write and verify immutable evidence files before database acceptance.

Content hashes identify bytes, not publications. Each write syncs a private
temporary file and installs it without replacing an existing object. Recovery
reports interrupted writes; it never deletes another worker's staged file.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path


class IntegrityFault(ValueError):
    """An accepted content reference cannot be verified."""


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class BlobStore:
    def __init__(self, root: Path, fault, *, read_only: bool = False) -> None:
        self.root = root
        self.read_only = read_only
        if not read_only:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.root.chmod(0o700)
        self.fault = fault

    def path(self, digest: str) -> Path:
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise IntegrityFault("Invalid content hash")
        return self.root / digest

    def read(self, digest: str) -> bytes:
        path = self.path(digest)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as handle:
                content = handle.read()
        except OSError as exc:
            raise IntegrityFault(f"Content object {digest} is unavailable") from exc
        if hashlib.sha256(content).hexdigest() != digest:
            raise IntegrityFault(f"Content object {digest} failed its hash check")
        return content

    def put(self, content: bytes) -> dict:
        if self.read_only:
            raise PermissionError("Evidence store was opened for inspection only")
        if not isinstance(content, bytes):
            raise TypeError("Evidence content must be bytes")
        digest = hashlib.sha256(content).hexdigest()
        target = self.path(digest)
        if target.exists() or target.is_symlink():
            self.read(digest)
            return {"sha256": digest, "size": len(content)}
        self.fault("before_blob_write")
        descriptor, name = tempfile.mkstemp(prefix=".staged-", dir=self.root)
        staged = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                self.fault("during_blob_write")
                handle.flush()
                os.fsync(handle.fileno())
            self.fault("after_blob_write")
            # A concurrent identical writer may win; never overwrite that object.
            try:
                os.link(staged, target)
            except FileExistsError:
                self.read(digest)
            sync_directory(self.root)
            self.fault("after_blob_install")
        finally:
            staged.unlink(missing_ok=True)
        return {"sha256": digest, "size": len(content)}

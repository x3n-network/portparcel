import ftplib
import hashlib
import os
import posixpath
import ssl
import tempfile
import uuid
from pathlib import Path, PurePosixPath

import paramiko

from .config import load_profile, read_config
from .errors import TransferError
from .transports import connect

OPERATIONS = {"list", "stat", "upload", "download", "mkdir", "rename", "delete"}
WRITES = {"upload", "mkdir", "rename", "delete"}


def remote_path(value, root):
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise TransferError("PATH_DENIED", "Invalid remote path.")
    if ".." in PurePosixPath(value).parts or value.startswith("//") or "\\" in value:
        raise TransferError("PATH_DENIED", "Remote traversal is not allowed.")
    path = posixpath.normpath(value if value.startswith("/") else posixpath.join(root, value))
    root = posixpath.normpath(root)
    if path != root and not path.startswith(root.rstrip("/") + "/"):
        raise TransferError("PATH_DENIED", "Remote path is outside configured root.")
    return path


def local_path(value, root):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise TransferError("PATH_DENIED", "Invalid local path.")
    base = Path(root).expanduser().resolve(strict=True)
    candidate = Path(value).expanduser()
    path = (candidate if candidate.is_absolute() else base / candidate).resolve()
    if path == base or not path.is_relative_to(base):
        raise TransferError("PATH_DENIED", "Local path is outside configured root.")
    return path


def require_file(item):
    if item is None:
        raise TransferError("NOT_FOUND", "Remote file does not exist.")
    if item["type"] != "file":
        raise TransferError("NOT_A_FILE", "Operation requires a regular file.")


def require_absent(item, overwrite):
    if item and not overwrite:
        raise TransferError("ALREADY_EXISTS", "Destination exists; explicit overwrite is required.")
    if item and item["type"] != "file":
        raise TransferError("NOT_A_FILE", "Cannot overwrite a non-file destination.")


class LimitedSource:
    def __init__(self, source, maximum):
        self.source, self.maximum = source, maximum
        self.count = 0
        self.digest = hashlib.sha256()

    def read(self, size):
        chunk = self.source.read(size)
        self.count += len(chunk)
        if self.count > self.maximum:
            raise TransferError("LIMIT_EXCEEDED", "Transfer exceeds configured max_bytes.")
        self.digest.update(chunk)
        return chunk


def perform(
    operation,
    name,
    config=None,
    remote_path_value=None,
    local_path_value=None,
    destination=None,
    overwrite=False,
    dry_run=False,
):
    if operation == "connections":
        return {"connections": sorted(read_config(config))}
    if operation not in OPERATIONS:
        raise TransferError("INVALID_ARGUMENT", "Unknown operation.")
    if type(overwrite) is not bool or type(dry_run) is not bool:
        raise TransferError("INVALID_ARGUMENT", "Flags must be booleans.")
    p = load_profile(name, config)
    path = remote_path(remote_path_value, p.remote_root)
    if operation in WRITES and p.read_only:
        raise TransferError("READ_ONLY", "Connection is configured read-only.")
    if operation in WRITES and path == posixpath.normpath(p.remote_root):
        raise TransferError("PATH_DENIED", "Cannot mutate the configured remote root.")
    local = (
        local_path(local_path_value, p.local_root) if operation in {"upload", "download"} else None
    )
    target = remote_path(destination, p.remote_root) if operation == "rename" else None
    if target == posixpath.normpath(p.remote_root):
        raise TransferError("PATH_DENIED", "Cannot replace the configured remote root.")
    if target == path:
        raise TransferError("INVALID_ARGUMENT", "Rename source and destination must differ.")
    with connect(p) as remote:
        remote.guard(path, p.remote_root)
        item = remote.stat(path)
        if operation == "stat":
            if item is None:
                raise TransferError("NOT_FOUND", "Remote path does not exist.")
            return {"path": path, **item}
        if operation == "list":
            if item is None or item["type"] != "directory":
                raise TransferError("NOT_A_DIRECTORY", "Remote path is not a directory.")
            return {"path": path, "entries": sorted(remote.list(path), key=lambda e: e["name"])}
        result = {"operation": operation, "remote_path": path, "dry_run": dry_run}
        if local:
            result["local_path"] = str(local)
        if operation == "upload":
            require_absent(item, overwrite)
            if not local.is_file():
                raise TransferError("NOT_A_FILE", "Local source must be a regular file.")
            if local.stat().st_size > p.max_bytes:
                raise TransferError("LIMIT_EXCEEDED", "Transfer exceeds configured max_bytes.")
            if dry_run:
                return result
            stage = posixpath.join(posixpath.dirname(path), ".portparcel-" + uuid.uuid4().hex)
            remote.guard(stage, p.remote_root)
            try:
                with local.open("rb") as source:
                    stream = LimitedSource(source, p.max_bytes)
                    remote.write(stage, stream)
                staged = remote.stat(stage)
                if not staged or staged["size"] != stream.count:
                    raise TransferError("VERIFY_FAILED", "Remote staged byte count differs.")
                require_absent(remote.stat(path), overwrite)
                remote.rename(stage, path, overwrite=overwrite)
                final = remote.stat(path)
                if not final or final["size"] != stream.count:
                    raise TransferError("VERIFY_FAILED", "Remote final byte count differs.")
                result.update(
                    bytes=stream.count, sha256=stream.digest.hexdigest(), verification="remote_size"
                )
            finally:
                try:
                    remote.delete(stage)
                except Exception:
                    pass
        elif operation == "download":
            require_file(item)
            if item["size"] > p.max_bytes:
                raise TransferError("LIMIT_EXCEEDED", "Transfer exceeds configured max_bytes.")
            if local.exists() and not overwrite:
                raise TransferError("ALREADY_EXISTS", "Local destination exists.")
            if not local.parent.is_dir() or (local.exists() and not local.is_file()):
                raise TransferError(
                    "INVALID_ARGUMENT", "Local parent must exist; target must be a file."
                )
            if dry_run:
                return result
            fd, stage = tempfile.mkstemp(prefix=".portparcel-", dir=local.parent)
            count, digest = 0, hashlib.sha256()
            try:
                with os.fdopen(fd, "wb") as output:

                    def sink(chunk):
                        nonlocal count
                        count += len(chunk)
                        if count > p.max_bytes:
                            raise TransferError(
                                "LIMIT_EXCEEDED", "Transfer exceeds configured max_bytes."
                            )
                        digest.update(chunk)
                        output.write(chunk)

                    remote.read(path, sink)
                    output.flush()
                    os.fsync(output.fileno())
                if count != item["size"]:
                    raise TransferError(
                        "VERIFY_FAILED", "Downloaded byte count differs from remote stat."
                    )
                if overwrite:
                    os.replace(stage, local)
                else:
                    os.link(stage, local)  # Atomic no-clobber publication on local filesystem.
                result.update(bytes=count, sha256=digest.hexdigest(), verification="received_size")
            finally:
                Path(stage).unlink(missing_ok=True)
        elif operation == "mkdir":
            if item:
                raise TransferError("ALREADY_EXISTS", "Remote destination exists.")
            if not dry_run:
                remote.mkdir(path)
                final = remote.stat(path)
                if not final or final["type"] != "directory":
                    raise TransferError(
                        "VERIFY_FAILED", "Remote directory creation was not observed."
                    )
        elif operation == "rename":
            require_file(item)
            remote.guard(target, p.remote_root)
            require_absent(remote.stat(target), overwrite)
            result["destination"] = target
            if not dry_run:
                remote.rename(path, target, overwrite)
                final = remote.stat(target)
                if remote.stat(path) is not None or not final or final["size"] != item["size"]:
                    raise TransferError("VERIFY_FAILED", "Remote rename was not observed.")
        elif operation == "delete":
            require_file(item)
            if not dry_run:
                remote.delete(path)
                if remote.stat(path) is not None:
                    raise TransferError("VERIFY_FAILED", "Remote deletion was not observed.")
        return result


def execute(
    operation,
    connection=None,
    *,
    config=None,
    remote_path=None,
    local_path=None,
    destination=None,
    overwrite=False,
    dry_run=False,
):
    """Shared JSON contract. Never return raw server exceptions or credentials."""
    try:
        result = perform(
            operation, connection, config, remote_path, local_path, destination, overwrite, dry_run
        )
        return {"ok": True, "result": result}
    except TransferError as exc:
        code, message = exc.code, str(exc)
    except FileExistsError:
        code, message = "ALREADY_EXISTS", "Destination already exists."
    except FileNotFoundError:
        code, message = "NOT_FOUND", "A required file or directory does not exist."
    except PermissionError:
        code, message = "PERMISSION_DENIED", "Permission denied."
    except (paramiko.BadHostKeyException, ssl.SSLCertVerificationError):
        code, message = "IDENTITY_ERROR", "Server identity verification failed."
    except (paramiko.AuthenticationException, ftplib.error_perm):
        code, message = "REMOTE_DENIED", "Remote authentication, permission, or command denied."
    except (OSError, EOFError, paramiko.SSHException, ftplib.Error, ssl.SSLError):
        code, message = (
            "CONNECTION_ERROR",
            "Connection or transfer failed; check server and trust settings.",
        )
    except Exception:
        code, message = "INTERNAL_ERROR", "Unexpected failure; no remote error text is exposed."
    return {"ok": False, "error": {"code": code, "message": message}}

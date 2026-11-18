import json
import os
from dataclasses import dataclass, fields
from pathlib import Path, PurePosixPath

from .errors import TransferError


def config_path() -> Path:
    return Path(
        os.environ.get("PORTPARCEL_CONFIG", "~/.config/portparcel/connections.json")
    ).expanduser()


@dataclass(frozen=True)
class Profile:
    host: str
    username: str
    remote_root: str
    local_root: str
    protocol: str = "sftp"
    port: int = 22
    read_only: bool = True
    allow_insecure_ftp: bool = False
    password_env: str | None = None
    key_filename: str | None = None
    key_passphrase_env: str | None = None
    known_hosts: str = "~/.ssh/known_hosts"
    ca_file: str | None = None
    timeout: float = 30
    max_bytes: int = 104857600


def read_config(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or config_path()).read_text())
        if not isinstance(data, dict) or not isinstance(data.get("connections"), dict):
            raise ValueError()
        return data["connections"]
    except (OSError, ValueError, TypeError):
        raise TransferError("CONFIG_ERROR", "Cannot read connections JSON configuration.") from None


def load_profile(name: str, path: Path | None = None) -> Profile:
    connections = read_config(path)
    if name not in connections:
        raise TransferError("UNKNOWN_CONNECTION", "Named connection does not exist.")
    raw = connections[name]
    allowed = {f.name for f in fields(Profile)}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise TransferError(
            "CONFIG_ERROR", "Unknown profile fields; store secrets by env reference."
        )
    try:
        raw = dict(raw)
        raw.setdefault("port", {"sftp": 22, "ftps": 21, "ftp": 21}[raw.get("protocol", "sftp")])
        p = Profile(**raw)
        for key in ("host", "username", "remote_root", "local_root", "known_hosts"):
            value = getattr(p, key)
            if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
                raise ValueError()
        if any(c in p.host for c in "/@"):
            raise ValueError()
        if not isinstance(p.port, int) or isinstance(p.port, bool) or not 1 <= p.port <= 65535:
            raise ValueError()
        if type(p.read_only) is not bool or type(p.allow_insecure_ftp) is not bool:
            raise ValueError()
        if not isinstance(p.timeout, (float, int)) or not 0 < p.timeout <= 300:
            raise ValueError()
        if type(p.max_bytes) is not int or not 0 < p.max_bytes <= 10737418240:
            raise ValueError()
        root = PurePosixPath(p.remote_root)
        if not root.is_absolute() or ".." in root.parts or str(root).startswith("//"):
            raise ValueError()
        if not Path(p.local_root).expanduser().is_absolute():
            raise ValueError()
        for key in ("password_env", "key_passphrase_env", "key_filename", "ca_file"):
            value = getattr(p, key)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise TransferError("CONFIG_ERROR", "Invalid connection profile settings.") from None
    if p.protocol == "ftp" and not p.allow_insecure_ftp:
        raise TransferError("INSECURE_PROTOCOL", "Plain FTP requires allow_insecure_ftp=true.")
    return p


def secret(env_name: str | None) -> str | None:
    if env_name is None:
        return None
    value = os.environ.get(env_name)
    if not value:
        raise TransferError(
            "MISSING_CREDENTIAL", "A configured credential environment variable is unset."
        )
    return value

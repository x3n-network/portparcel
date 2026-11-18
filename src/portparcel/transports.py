"""Thin protocol adapters. TLS and SSH identity checks cannot be disabled."""

import ftplib
import posixpath
import ssl
import stat
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import paramiko

from .config import secret
from .errors import TransferError


def entry(name, mode, size):
    kind = {
        stat.S_IFLNK: "symlink",
        stat.S_IFDIR: "directory",
        stat.S_IFREG: "file",
    }.get(stat.S_IFMT(mode or 0), "unknown")
    return {"name": name, "type": kind, "size": size}


class SFTP:
    def __init__(self, client):
        self.client = client

    def stat(self, path):
        try:
            a = self.client.lstat(path)
        except FileNotFoundError:
            return None
        return entry(posixpath.basename(path), a.st_mode, a.st_size)

    def guard(self, path, root):
        # Deny every symbolic-link component, including configured root components.
        parts = path.strip("/").split("/")
        current = ""
        for part in parts:
            if not part:
                continue
            current += "/" + part
            item = self.stat(current)
            if item and item["type"] == "symlink":
                raise TransferError("PATH_DENIED", "Remote symbolic links are not allowed.")

    def list(self, path):
        result = []
        for a in self.client.listdir_iter(path):
            result.append(entry(a.filename, a.st_mode, a.st_size))
            if len(result) > 10000:
                raise TransferError("LIMIT_EXCEEDED", "Directory exceeds 10000 entries.")
        return result

    def read(self, path, sink):
        with self.client.open(path, "rb") as source:
            while chunk := source.read(65536):
                sink(chunk)

    def write(self, path, source):
        with self.client.open(path, "wx") as target:
            while chunk := source.read(65536):
                target.write(chunk)

    def mkdir(self, path):
        self.client.mkdir(path)

    def rename(self, source, target, overwrite=False):
        if overwrite:
            self.client.posix_rename(source, target)
        else:
            self.client.rename(source, target)

    def delete(self, path):
        self.client.remove(path)


class SessionFTP_TLS(ftplib.FTP_TLS):
    """Offer the verified control session on each protected data connection."""

    def ntransfercmd(self, cmd, rest=None):
        if not self._prot_p or not isinstance(self.sock, ssl.SSLSocket):
            raise TransferError("CONNECTION_ERROR", "FTPS requires protected data connections.")
        # Bypass FTP_TLS.ntransfercmd, which wraps the socket without a session.
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        try:
            conn = self.context.wrap_socket(
                conn, server_hostname=self.host, session=self.sock.session
            )
        except Exception:
            conn.close()
            self.close()  # Do not reuse a control channel with a pending transfer reply.
            raise
        return conn, size


class FTP:
    def __init__(self, client, root="/"):
        self.client = client
        self.root = posixpath.normpath(root)

    def list(self, path):
        result = []

        def add_line(line):
            fact_text, separator, name = line.partition(" ")
            if not separator or not name:
                raise TransferError("CONNECTION_ERROR", "Invalid remote directory listing.")
            facts = dict(
                (key.lower(), value)
                for fact in fact_text.split(";")
                for key, equals, value in (fact.partition("="),)
                if equals
            )
            kind = facts.get("type", "").lower()
            if kind in ("cdir", "pdir"):
                return
            try:
                size = int(facts["size"]) if "size" in facts else None
                if size is not None and size < 0:
                    raise ValueError("negative size")
            except ValueError:
                raise TransferError(
                    "CONNECTION_ERROR", "Invalid remote directory listing."
                ) from None
            result.append(
                {
                    "name": name,
                    "type": "directory"
                    if kind == "dir"
                    else "file"
                    if kind == "file"
                    else "unknown",
                    "size": size,
                }
            )
            if len(result) > 10000:
                raise TransferError("LIMIT_EXCEEDED", "Directory exceeds 10000 entries.")

        # ftplib.mlsd buffers the entire listing before yielding the first row.
        try:
            self.client.retrlines(f"MLSD {path}", add_line)
        except Exception:
            # An interrupted data transfer can leave a pending control reply.
            self.client.close()
            raise
        return result

    def stat(self, path):
        if path == self.root:
            # Verify the configured directory directly; its parent may be unlistable.
            # All subsequent FTP commands use absolute paths on this private connection.
            self.client.cwd(path)
            return {"name": posixpath.basename(path) or "/", "type": "directory", "size": 0}
        parent, name = posixpath.split(path)
        item = next((e for e in self.list(parent or "/") if e["name"] == name), None)
        if item and item["type"] == "file" and item["size"] is None:
            try:
                self.client.voidcmd("TYPE I")
                size = self.client.size(path)
            except (ftplib.error_perm, ValueError):
                size = None
            if size is None or size < 0:
                raise TransferError(
                    "METADATA_UNAVAILABLE", "Server did not supply the remote file size."
                )
            item["size"] = size
        return item

    def guard(self, path, root):
        # FTP lacks a portable realpath/lstat: server-side jail is mandatory.
        pass

    def read(self, path, sink):
        self.client.retrbinary("RETR " + path, sink, blocksize=65536)

    def write(self, path, source):
        self.client.storbinary("STOR " + path, source, blocksize=65536)

    def mkdir(self, path):
        self.client.mkd(path)

    def rename(self, source, target, overwrite=False):
        self.client.rename(source, target)

    def delete(self, path):
        self.client.delete(path)


class StrictHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        raise TransferError("HOST_KEY_ERROR", "Server host key is not present in known_hosts.")


def open_sftp(ssh, timeout):
    channel = ssh.get_transport().open_session(timeout=timeout)
    channel.settimeout(timeout)
    # Paramiko's subsystem acknowledgment wait does not use the channel's I/O timeout.
    requested = Future()

    def request_subsystem():
        try:
            channel.invoke_subsystem("sftp")
        except Exception as exc:
            requested.set_exception(exc)
        else:
            requested.set_result(None)

    Thread(target=request_subsystem, name="portparcel-sftp-request", daemon=True).start()
    try:
        requested.result(timeout=timeout)
    except TimeoutError:
        # The caller closes the dedicated SSH connection, releasing the waiting thread.
        raise TimeoutError("SFTP subsystem request timed out.") from None
    return paramiko.SFTPClient(channel)


@contextmanager
def connect(p):
    password = secret(p.password_env)
    if p.protocol == "sftp":
        ssh = paramiko.SSHClient()
        try:
            known = Path(p.known_hosts).expanduser()
            if not known.is_file():
                raise TransferError("HOST_KEY_ERROR", "Configured known_hosts file is missing.")
            ssh.load_host_keys(str(known))
            ssh.set_missing_host_key_policy(StrictHostKeyPolicy())
            ssh.connect(
                p.host,
                port=p.port,
                username=p.username,
                password=password,
                key_filename=str(Path(p.key_filename).expanduser()) if p.key_filename else None,
                passphrase=secret(p.key_passphrase_env),
                timeout=p.timeout,
                banner_timeout=p.timeout,
                auth_timeout=p.timeout,
                allow_agent=password is None and p.key_filename is None,
                look_for_keys=password is None and p.key_filename is None,
            )
            with open_sftp(ssh, p.timeout) as client:
                yield SFTP(client)
        finally:
            ssh.close()
    else:
        if p.protocol == "ftps":
            context = ssl.create_default_context(
                cafile=str(Path(p.ca_file).expanduser()) if p.ca_file else None
            )
            client = SessionFTP_TLS(context=context, timeout=p.timeout)
        else:
            client = ftplib.FTP(timeout=p.timeout)
        try:
            client.connect(p.host, p.port)
            client.login(p.username, password or "")
            if p.protocol == "ftps":
                client.prot_p()
            client.set_pasv(True)
            yield FTP(client, root=p.remote_root)
        finally:
            client.close()

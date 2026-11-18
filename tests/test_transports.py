import ftplib
import stat
import threading
from types import SimpleNamespace

import paramiko

import pytest

from portparcel.errors import TransferError
from portparcel.transports import FTP, connect, entry


def test_sftp_entry_distinguishes_special_files():
    assert entry("regular", stat.S_IFREG, 1)["type"] == "file"
    assert entry("special", stat.S_IFIFO, 0)["type"] == "unknown"


def test_ftp_listing_stops_reading_at_the_entry_limit():
    class ListingClient:
        read_count = 0

        def close(self):
            pass

        def mlsd(self, path):
            # Mirror ftplib.mlsd: receive the whole listing before yielding any rows.
            rows = []
            self.retrlines(
                f"MLSD {path}", lambda line: rows.append((line.split(" ", 1)[1], {"type": "file"}))
            )
            yield from rows

        def retrlines(self, command, callback):
            assert command == "MLSD /files"
            for i in range(10020):
                self.read_count += 1
                callback(f"type=file;size=1; file-{i}")

    client = ListingClient()
    with pytest.raises(TransferError) as exc:
        FTP(client).list("/files")
    assert exc.value.code == "LIMIT_EXCEEDED"
    assert client.read_count == 10001


def test_ftp_missing_size_is_unknown_in_listing_and_resolved_for_stat():
    class Client:
        def retrlines(self, command, callback):
            assert command == "MLSD /test"
            callback("type=file; payload.bin")
            callback("type=file;size=0; empty.bin")

        def voidcmd(self, command):
            assert command == "TYPE I"
            self.binary = True

        def size(self, path):
            assert self.binary
            assert path == "/test/payload.bin"
            return 12

    ftp = FTP(Client())
    assert [item["size"] for item in ftp.list("/test")] == [None, 0]
    assert ftp.stat("/test/payload.bin")["size"] == 12
    assert ftp.stat("/test/empty.bin")["size"] == 0


def test_ftp_stat_refuses_to_invent_size_if_size_command_is_unavailable():
    class Client:
        def retrlines(self, command, callback):
            callback("type=file; payload.bin")

        def voidcmd(self, command):
            pass

        def size(self, path):
            raise ftplib.error_perm("502 SIZE not implemented")

    with pytest.raises(TransferError) as exc:
        FTP(Client()).stat("/test/payload.bin")
    assert exc.value.code == "METADATA_UNAVAILABLE"


def test_sftp_subsystem_uses_profile_timeout_and_closes_ssh(monkeypatch, tmp_path):
    closed = threading.Event()
    started = threading.Event()
    finished = threading.Event()

    class Channel:
        def settimeout(self, timeout):
            assert timeout == 0.1

        def invoke_subsystem(self, name):
            assert name == "sftp"
            started.set()
            closed.wait(2)
            finished.set()
            raise paramiko.SSHException("channel closed")

    class Client:
        def load_host_keys(self, filename):
            pass

        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, host, **kwargs):
            pass

        def get_transport(self):
            return self

        def open_session(self, *, timeout):
            assert timeout == 0.1
            return Channel()

        def open_sftp(self):
            raise AssertionError("The unbounded convenience method must not be called")

        def close(self):
            closed.set()

    known_hosts = tmp_path / "known_hosts"
    known_hosts.touch()
    monkeypatch.setattr(paramiko, "SSHClient", Client)
    p = SimpleNamespace(
        protocol="sftp",
        password_env=None,
        known_hosts=str(known_hosts),
        host="localhost",
        port=22,
        username="tester",
        key_filename=None,
        key_passphrase_env=None,
        timeout=0.1,
    )
    with pytest.raises(TimeoutError, match="SFTP subsystem"):
        with connect(p):
            pytest.fail("A stalled subsystem must not yield a client")
    assert started.is_set() and closed.is_set() and finished.wait(1)

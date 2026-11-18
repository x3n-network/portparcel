# Changelog

## v0.1.0 — initial prerelease

- Transfer individual files over SFTP, explicit FTPS, or opt-in plaintext FTP through a JSON CLI or a stdio MCP server.
- Configure named, read-only-by-default profiles; use dry runs and explicit overwrite for writes. SSH host key and TLS certificate verification stay enabled.
- FTPS supports TLS session reuse for servers that require it on protected data connections.
- SFTP setup honors the configured timeout. FTP/FTPS can inspect the configured root without listing its parent, and never substitute zero for an unknown file size.
- Live checks covered SFTP and FTPS listing and downloads. A live FTPS smoke test also verified upload, byte-for-byte download, rename, deletion, and cleanup in a dedicated test directory.

FTP/FTPS directory confinement must be enforced by the server. See the [security policy](SECURITY.md) and [documented limitations](README.md#safety-boundaries-and-limitations).

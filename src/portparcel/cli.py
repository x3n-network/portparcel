import argparse
import json
from pathlib import Path

from .service import execute


class Parser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"ok": False, "error": {"code": "INVALID_ARGUMENT", "message": message}}))
        raise SystemExit(2)


def main():
    parser = Parser(
        description="Agent-first SFTP/FTPS/FTP. All operation output is JSON.",
        epilog="Made by X3N LLC · https://x3n.network",
    )
    parser.add_argument("--config", type=Path, help="Connections JSON (or PORTPARCEL_CONFIG)")
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("connections", help="List configured connection names (no credentials)")
    for name in ("list", "stat", "upload", "download", "mkdir", "rename", "delete"):
        sub = commands.add_parser(name)
        sub.add_argument("connection")
        if name == "upload":
            sub.add_argument("local_path")
            sub.add_argument("remote_path")
        else:
            sub.add_argument("remote_path")
            if name == "download":
                sub.add_argument("local_path")
            if name == "rename":
                sub.add_argument("destination")
        if name in ("upload", "download", "rename"):
            sub.add_argument("--overwrite", action="store_true")
        if name in ("upload", "download", "mkdir", "rename", "delete"):
            sub.add_argument("--dry-run", action="store_true")
    response = execute(**vars(parser.parse_args()))
    print(json.dumps(response, ensure_ascii=True))
    raise SystemExit(0 if response["ok"] else 1)


if __name__ == "__main__":
    main()

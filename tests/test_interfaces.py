import json
import os
import subprocess

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytest_plugins = ["test_integration"]


def test_cli_round_trip(endpoint):
    config, local, remote, _ = endpoint
    (local / "cli.txt").write_text("real CLI transfer")
    command = [
        str(__import__("sys").executable),
        "-m",
        "portparcel.cli",
        "--config",
        str(config),
    ]
    upload = subprocess.run(
        command + ["upload", "test", "cli.txt", "/cli.txt"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert upload.returncode == 0, upload.stderr + upload.stdout
    assert json.loads(upload.stdout)["ok"]
    assert (remote / "cli.txt").read_text() == "real CLI transfer"
    download = subprocess.run(
        command + ["download", "test", "/cli.txt", "copy.txt"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert download.returncode == 0, download.stdout
    assert (local / "copy.txt").read_text() == "real CLI transfer"
    error = subprocess.run(
        command + ["delete", "test", "/absent"], capture_output=True, text=True, timeout=20
    )
    assert error.returncode == 1
    assert json.loads(error.stdout)["error"]["code"] == "NOT_FOUND"


def test_mcp_real_stdio_transfer(endpoint):
    config, local, remote, _ = endpoint
    (local / "mcp.txt").write_text("real MCP transfer")

    async def run():
        params = StdioServerParameters(
            command=__import__("sys").executable,
            args=["-m", "portparcel.server"],
            env={**os.environ, "PORTPARCEL_CONFIG": str(config)},
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert names == {
                    "connections",
                    "list_directory",
                    "stat",
                    "upload",
                    "download",
                    "mkdir",
                    "rename",
                    "delete",
                }

                async def call(name, args):
                    result = await session.call_tool(name, args)
                    assert not result.isError, result
                    data = json.loads(result.content[0].text)
                    assert data["ok"], data
                    return data

                await call("connections", {})
                await call(
                    "upload",
                    {"connection": "test", "local_path": "mcp.txt", "remote_path": "/mcp.txt"},
                )
                assert (remote / "mcp.txt").read_text() == "real MCP transfer"
                await call(
                    "download",
                    {"connection": "test", "remote_path": "/mcp.txt", "local_path": "mcp-copy.txt"},
                )
                assert (local / "mcp-copy.txt").read_text() == "real MCP transfer"
                await call("stat", {"connection": "test", "remote_path": "/mcp.txt"})
                await call("list_directory", {"connection": "test", "remote_path": "/"})
                await call("mkdir", {"connection": "test", "remote_path": "/mcp-dir"})
                await call(
                    "rename",
                    {
                        "connection": "test",
                        "remote_path": "/mcp.txt",
                        "destination": "/mcp-dir/mcp.txt",
                    },
                )
                await call("delete", {"connection": "test", "remote_path": "/mcp-dir/mcp.txt"})
                assert not (remote / "mcp-dir/mcp.txt").exists()

    anyio.run(run)


def test_cli_argument_errors_are_json():
    result = subprocess.run(
        [__import__("sys").executable, "-m", "portparcel.cli", "unknown"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "INVALID_ARGUMENT"

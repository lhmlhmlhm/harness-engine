"""The MCP server, driven by a REAL client.

WHY A REAL CLIENT AND NOT A HAND-DRIVEN SOCKET

Speaking JSON-RPC at the server myself would prove the server is consistent with my reading of
the protocol, which is the claim least worth making — the whole reason this integration takes a
dependency on the `mcp` package rather than hand-rolling the wire format is that conformance
should not be something this project asserts about itself. So the tests connect with the SDK's
own client and go through initialize / tools/list / tools/call.

WHY THIS FILE SKIPS INSTEAD OF FAILING

The engine's own suite must not require an integration's dependency. `mcp` is needed by the MCP
server and by nothing else, so a machine without it should see these skipped and the other
suites green — not a red build that looks like the engine is broken.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="the MCP server's own dependency; the engine does not need it")

REPO = Path(__file__).resolve().parents[1]
SERVER = REPO / "integrations" / "mcp-server.py"
OK, USAGE, REFUSED = 0, 1, 3


def _state(tmp_path: Path) -> dict:
    """An initialised store of its own, and a run to drive."""
    env = {**os.environ, "HARNESS_STATE_DIR": str(tmp_path / "state")}
    env.pop("HARNESS_ABILITIES_PATH", None)
    exe = [sys.executable, str(REPO / "bin" / "harness")]
    assert subprocess.run([*exe, "init"], env=env, capture_output=True).returncode == OK
    assert subprocess.run([*exe, "open", "delivery", "--scope", str(tmp_path / "repo"),
                           "--run", "m1"], env=env, capture_output=True).returncode == OK
    return env


def _drive(env: dict, body):
    """Connect with the SDK's client, hand the session to `body`, return its result."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=[str(SERVER)],
            env={**env, "HARNESS_ENGINE": str(REPO)})
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                return await body(session)

    return asyncio.run(run())


def test_the_tools_are_the_engines_own_surface(tmp_path):
    """The list must BE the published surface, not agree with it by coincidence.

    Asserted as set equality against `adapter-contract`, so a tool this server invented and a
    command it silently dropped both fail. The exclusions are asserted as absences for the same
    reason: a list that merely happens to omit them today is not an exclusion.
    """
    env = _state(tmp_path)
    contract = json.loads(subprocess.run(
        [sys.executable, str(REPO / "bin" / "harness"), "adapter-contract"],
        env=env, capture_output=True, text=True).stdout)
    surface = contract["surface"]

    async def body(session):
        return (await session.list_tools()).tools

    tools = _drive(env, body)
    assert {t.name for t in tools} == {t["tool"] for t in surface["tools"]}

    for command in surface["not_a_tool"]:
        banned = "harness_" + command.replace("-", "_")
        assert banned not in {t.name for t in tools}, \
            f"{command} is offered as a tool although the engine says it must not be"

    # Every tool must say that an exit code is an answer, or a runtime reading only the
    # description will treat 3 and 4 as malfunctions.
    for t in tools:
        assert "not a failure" in t.description, t.name


def test_the_json_flag_is_used_but_never_offered(tmp_path):
    """A parameter whose only use is to make the answer harder to read is not a parameter.

    For a command that can answer in JSON the server always asks for JSON, so `--json` is absent
    from the schema AND present in the call. Both halves are asserted: omitting it from the
    schema while also not passing it would leave the model parsing prose.
    """
    env = _state(tmp_path)

    async def body(session):
        tools = {t.name: t for t in (await session.list_tools()).tools}
        res = await session.call_tool("harness_next", {"run": "m1"})
        return tools, json.loads(res.content[0].text)

    tools, payload = _drive(env, body)
    assert "json" not in tools["harness_next"].inputSchema["properties"]
    assert "--json" in payload["argv"], payload["argv"]
    assert isinstance(payload.get("data"), dict), payload
    assert payload["data"]["step"] and payload["data"]["requirements"]


def test_a_refusal_comes_back_as_data_and_not_as_a_broken_tool(tmp_path):
    """Exit 3 is the product. Reported as a tool error, it invites a retry instead of a read.

    So the result is NOT flagged as an error, the code and its name ride in the payload, and the
    engine's stderr — which names what is missing and the command that supplies it — is carried
    through rather than summarised.
    """
    env = _state(tmp_path)

    async def body(session):
        nxt = json.loads((await session.call_tool(
            "harness_next", {"run": "m1"})).content[0].text)
        res = await session.call_tool(
            "harness_close_step", {"run": "m1", "step": nxt["data"]["step"]})
        return res, json.loads(res.content[0].text)

    res, payload = _drive(env, body)
    assert payload["exit"] == REFUSED
    assert payload["meaning"] == "REFUSED"
    assert res.isError is False, "a refusal was reported as a tool malfunction"
    assert "REFUSED" in payload["stderr"]


def test_an_excluded_command_appearing_as_a_tool_stops_the_server(tmp_path):
    """The exclusion is enforced on BOTH sides, because one side is one refactor from gone.

    The engine omits these from the surface; this server also refuses to serve if it ever finds
    one there. Driven by a stub engine that publishes a contract naming `guard-tool` as both a
    tool and a thing that must not be one — the contradiction a producer-only check would serve
    happily.
    """
    stub_root = tmp_path / "stub"
    (stub_root / "bin").mkdir(parents=True)
    stub = stub_root / "bin" / "harness"
    contract = {
        "surface": {
            "tools": [{"command": "guard-tool", "tool": "harness_guard_tool", "help": "h",
                       "structured": False, "args": []}],
            "not_a_tool": {"guard-tool": "invoked by a runtime, never by the model, and so on."},
            "exit_codes": {"0": "OK"},
            "result_shape": {"note": "x", "fields": []},
        }
    }
    # Embedded as a python STRING literal, not as source: JSON's `false` is not a python name,
    # and a stub that fails to run would exercise the missing-engine path instead of this one.
    stub.write_text("#!/usr/bin/env python3\nprint("
                    + repr(json.dumps(contract)) + ")\n", encoding="utf-8")
    stub.chmod(0o755)

    proc = subprocess.run([sys.executable, str(SERVER)],
                          env={**os.environ, "HARNESS_ENGINE": str(stub_root)},
                          input="", capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0, "the server served a tool the engine forbids"
    assert "refusing to serve" in proc.stderr, proc.stderr
    assert "guard-tool" in proc.stderr


def test_a_missing_engine_is_named_rather_than_hung(tmp_path):
    """Startup must fail loudly: a server that connects and offers nothing looks like an engine
    with no commands, which is the same class of silence this project keeps removing."""
    proc = subprocess.run([sys.executable, str(SERVER)],
                          env={**os.environ, "HARNESS_ENGINE": str(tmp_path / "nowhere")},
                          input="", capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0
    assert "harness engine not found" in proc.stderr
    assert "HARNESS_ENGINE" in proc.stderr

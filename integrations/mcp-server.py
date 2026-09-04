#!/usr/bin/env python3
"""MCP server — the engine's command surface as tools, DERIVED, never restated.

WHY THIS EXISTS AT ALL

An agent driving this engine has to know what to call. Until now it learned that from prose:
first a hand-written driving document, which drifted from the engine and so became generated;
then the generated copy on one machine aged past the engine anyway, one line at a time. Tool
schemas are the same information in a form that cannot have a stale copy, because the runtime
asks for them at connect time and there is nothing to keep in sync.

WHAT IS DERIVED, AND FROM WHERE

Everything. `harness adapter-contract` publishes `surface`, which the ENGINE builds by reading
its own argparse parser. This file adds no knowledge of any command: not a name, not a flag,
not which commands support structured output. A schema this file wrote by hand would be the
third copy of the surface, and the previous two both aged.

So a new command appears here the moment it exists in the parser, and a removed flag disappears
from the schema without anyone editing this file.

WHY IT SHELLS OUT INSTEAD OF IMPORTING THE ENGINE

Each tool call runs `bin/harness` as a fresh process, which is exactly how a human drives it.
That keeps four properties a long-lived in-process engine would quietly weaken:

  * the trust check on an ability's `providers.py` runs on EVERY call, not once per server
    lifetime — an approved file edited after start would otherwise keep running unchecked
  * a spec edited mid-run is re-read and its digest re-compared on every call
  * a crash leaves nothing half-built in memory
  * there is no cached parse to invalidate, so there is no invalidation bug

The cost is real and small: about 200 ms per call, against model turns measured in seconds.
Latency is not why anyone would want this; the schema is.

WHAT IS DELIBERATELY NOT A TOOL

`NOT_A_TOOL` in the engine, with a reason per entry — `guard-tool` above all, because it is
invoked by a RUNTIME before a call it may block, and a model will not call a tool whose purpose
is to stop it. Handing it over as a tool would replace the one mechanism that does not need the
agent's cooperation with one that does. `trust` is excluded for the same class of reason: a
model that can approve extension code makes the content pinning decorative.

This file re-checks the exclusion at startup and refuses to serve if an excluded command ever
shows up in the tool list, rather than trusting that it never will.

AN EXIT CODE IS THE PRODUCT, NOT AN ERROR

3 (refused) and 4 (blocked) are answers. They are returned as data — `{"exit": 3, "meaning":
"REFUSED", ...}` — and NOT flagged as tool errors, because a runtime that reports a refusal as
a malfunction invites the model to retry the call instead of reading what is missing. Only an
adapter-level failure (unknown tool, the engine could not be run) is an error.

RUN IT

    HARNESS_ENGINE=<path-to-engine> python3 integrations/mcp-server.py

Requires the `mcp` package. That dependency is the deliberate half of a trade: hand-rolling the
protocol would mean claiming a conformance no test here could check, and an unverifiable claim
is worth less than a dependency.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

SERVER_NAME = "harness"
CALL_TIMEOUT_S = 60


def engine_root() -> Path:
    """Same two rules as the preToolUse adapter: the variable, else the checkout above this."""
    env = os.environ.get("HARNESS_ENGINE")
    return Path(env).expanduser() if env else Path(__file__).resolve().parents[1]


def harness_bin() -> Path:
    return engine_root() / "bin" / "harness"


def load_surface() -> dict:
    """Ask the engine what it offers. One subprocess, at startup."""
    exe = harness_bin()
    if not exe.exists():
        raise SystemExit(
            f"harness engine not found at {engine_root()}.\n"
            f"  Set HARNESS_ENGINE to the checkout that holds bin/harness."
        )
    proc = subprocess.run([sys.executable, str(exe), "adapter-contract"],
                          capture_output=True, text=True, timeout=CALL_TIMEOUT_S)
    if proc.returncode != 0:
        raise SystemExit(f"could not read the engine's contract (exit {proc.returncode}):\n"
                         f"{proc.stderr}")
    surface = json.loads(proc.stdout)["surface"]

    # Belt and braces: the surface already omits these, and this refuses to serve if that ever
    # stops being true. An exclusion enforced only by the producer is one refactor from gone.
    excluded = set(surface["not_a_tool"])
    offered = {t["command"] for t in surface["tools"]}
    leaked = excluded & offered
    if leaked:
        raise SystemExit(
            f"refusing to serve: {', '.join(sorted(leaked))} appear as tools, and the engine "
            f"itself lists them as things a model must not be handed.\n"
            f"  {'; '.join(surface['not_a_tool'][c] for c in sorted(leaked))}"
        )
    return surface


def input_schema(entry: dict) -> dict:
    """Argparse parameters -> JSON Schema. `--json` is not a parameter; see below."""
    props: dict[str, Any] = {}
    required: list[str] = []
    for a in entry["args"]:
        if a["flag"] == "--json":
            # Never exposed. For a command that can answer in JSON this server always asks for
            # JSON, so the only thing the parameter could do is let the model choose the shape
            # that is harder for it to read.
            continue
        schema: dict[str, Any] = {"description": a["help"] or a["name"]}
        if a["kind"] == "boolean":
            schema["type"] = "boolean"
        elif a["kind"] == "array":
            schema["type"] = "array"
            schema["items"] = {"type": "string"}
        else:
            schema["type"] = "string"
        if a["choices"]:
            schema["enum"] = a["choices"]
        props[a["name"]] = schema
        if a["required"]:
            required.append(a["name"])
    return {"type": "object", "properties": props, "required": required}


def describe(entry: dict, exit_codes: dict) -> str:
    codes = ", ".join(f"{k}={v}" for k, v in sorted(exit_codes.items()))
    tail = ("Answers as JSON." if entry["structured"] else "Answers as text.")
    return (f"{entry['help']}\n\n{tail} The result carries the engine's exit code as data — an "
            f"exit code is the answer, not a failure ({codes}). Read stderr on a refusal: it "
            f"names what is missing and the command that supplies it.")


def build_argv(entry: dict, arguments: dict) -> list[str]:
    argv = [entry["command"]]
    for a in entry["args"]:
        if a["flag"] == "--json":
            if entry["structured"]:
                argv.append("--json")
            continue
        if a["name"] not in arguments or arguments[a["name"]] in (None, ""):
            continue
        value = arguments[a["name"]]
        if a["kind"] == "boolean":
            if value:
                argv.append(a["flag"])
            continue
        values = value if isinstance(value, list) else [value]
        if a["flag"] is None:
            argv.extend(str(v) for v in values)      # positional
        else:
            for v in values:
                argv.extend([a["flag"], str(v)])
    return argv


def invoke(entry: dict, arguments: dict, exit_codes: dict) -> dict:
    argv = build_argv(entry, arguments)
    try:
        proc = subprocess.run([sys.executable, str(harness_bin()), *argv],
                              capture_output=True, text=True, timeout=CALL_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as exc:
        # An adapter-level failure, unlike a refusal: the engine never got to answer.
        return {"adapter_error": f"{type(exc).__name__}: {exc}", "argv": argv}
    payload: dict[str, Any] = {
        "exit": proc.returncode,
        "meaning": exit_codes.get(str(proc.returncode), "UNKNOWN"),
        "stderr": proc.stderr,
        "argv": argv,
    }
    if entry["structured"] and proc.returncode == 0 and proc.stdout.strip():
        try:
            payload["data"] = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload["stdout"] = proc.stdout       # honest fallback, never a silent swallow
    else:
        payload["stdout"] = proc.stdout
    return payload


def main() -> int:
    try:
        from mcp.server.lowlevel import Server
        from mcp.server.stdio import stdio_server
        import mcp.types as types
    except ImportError:
        print("the `mcp` package is required: pip install mcp", file=sys.stderr)
        return 1

    surface = load_surface()
    by_tool = {t["tool"]: t for t in surface["tools"]}
    exit_codes = surface["exit_codes"]
    server = Server(SERVER_NAME)

    @server.list_tools()
    async def _list() -> list[types.Tool]:
        return [types.Tool(name=t["tool"], description=describe(t, exit_codes),
                           inputSchema=input_schema(t))
                for t in surface["tools"]]

    @server.call_tool()
    async def _call(name: str, arguments: dict | None) -> list[types.TextContent]:
        entry = by_tool.get(name)
        if entry is None:
            raise ValueError(f"unknown tool {name!r}; this engine offers "
                             f"{', '.join(sorted(by_tool))}")
        payload = invoke(entry, arguments or {}, exit_codes)
        return [types.TextContent(type="text",
                                 text=json.dumps(payload, ensure_ascii=False, indent=2))]

    async def serve() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())

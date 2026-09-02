#!/usr/bin/env python3
"""fleet_probe.py — deterministic, transcript-independent verification of fleet reporting.

WHY THIS EXISTS (issue-004, twice)
----------------------------------
`hook-done`'s evidence check (v1.49.55) answers "did the agent execute the fleet contract?"
by counting `report_progress` / `upload_artifact` occurrences in the agent's own kiro-cli
session jsonl. That signal has two structural holes, and both were hit:

  1. The jsonl is often a 0-byte placeholder (fleet ACP pool worker; also observed on a plain
     local run — session bac0c73f). `fleet_call_count_ex` then returns `(-1, None)` and the
     check FAILS OPEN by design ("a verifier that cannot see must not block work").
  2. `--noop "<free text>"` short-circuits the whole check. The reason is never validated, so
     a *false* claim ("no agent-fleet MCP in this session") passes silently — and unlike a
     forgotten ack, which leaves a pending marker for `_reconcile_fleet_hooks` to catch, a
     false noop deletes the marker and looks like a clean run.

Measured cost of hole 2: ship-check 1a7c0807 made ZERO report_progress / upload_artifact calls
across an entire 8-layer session while acking 8 hooks with that exact false reason. The tools
were available the whole time; one read-only call would have disproved it.

THE FIX: VERIFY THE EFFECT AT THE DESTINATION, NOT THE INTENT IN THE TRANSCRIPT
-------------------------------------------------------------------------------
The fleet Central service exposes a plain, unauthenticated HTTP API on the same host as the
`agent-fleet` MCP endpoint. It is the actual store the MCP writes into, so it answers the real
question directly:

    GET /api/tasks/{task_id}/events      -> the progress events that ARRIVED
    GET /api/artifacts/{task_id}         -> the artifacts that ARRIVED
    GET /api/agents                      -> liveness

An agent cannot fake this: to make the check pass, the event must exist in Central. And the
one legitimate NO-OP condition ("fleet is not reachable from here") stops being an agent claim
and becomes an exit code.

⚠️ THE CRITICAL DESIGN POINT: "UNREACHABLE" vs "INDETERMINATE"
--------------------------------------------------------------
A probe that grants the NO-OP must never confuse "the fleet is down" with "I could not look".
The first draft of this file used `urllib`, which fails on this very host with
`CERTIFICATE_VERIFY_FAILED`: the corp CA lives in the macOS system keychain and is in NO pem
bundle on disk (checked /etc/ssl/cert.pem, homebrew's, certifi's). `curl` succeeds because it
links SecureTransport. Had that draft shipped, every probe would have reported "unreachable"
for a perfectly healthy Central and thereby **auto-granted the exact NO-OP this file exists to
withdraw** — a gate failing open on its own transport error (anti-pattern
`anti-gate-fails-open-on-own-load-error`).

So reachability is THREE-valued, and only positive evidence of absence relaxes anything:

    reachable      curl exit 0 + HTTP 200                     -> 0
    unreachable    curl exit 6/7/28 (DNS / refused / timeout)  -> 3   legitimate NO-OP
    indeterminate  TLS trust failure, curl missing, HTTP != 200 -> 4  NO-OP NOT granted

`4` is not a synonym for `3`. A caller seeing `4` must fall back to its previous check and say
out loud that it could not verify — never treat it as licence to skip the contract.

EXIT CODES (the contract — callers branch on these, not on prose)
-----------------------------------------------------------------
    reachable :  0 = reachable   3 = positively unreachable   4 = indeterminate
    snapshot  :  0 = printed     3 = positively unreachable   4 = indeterminate
    delta     :  0 = grew        2 = reachable, NO growth
                                 3 = positively unreachable   4 = indeterminate
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

TIMEOUT_SEC = 8
# Reachability is stable within a session and each probe costs ~1.3s over the corp network.
# 7 layer boundaries would otherwise add ~9s of pure latency, which is the kind of friction
# that gets a guard disabled. Only the POSITIVE result is cached (see check_reachable).
REACHABLE_CACHE_TTL_SEC = 60
CACHE_DIR = Path(os.path.expanduser("~/.kiro/skills/ship-check/state"))
CACHE_FILE = CACHE_DIR / ".fleet-reachable-cache.json"

MCP_SETTINGS = [
    Path(os.path.expanduser("~/.kiro/settings/mcp.json")),
    Path(os.path.expanduser("~/.aws/amazonq/mcp.json")),
]

# Verdict constants — the only vocabulary callers should branch on.
REACHABLE = "reachable"
UNREACHABLE = "unreachable"       # positive evidence of absence -> NO-OP is legitimate
INDETERMINATE = "indeterminate"   # we could not look -> NO-OP is NOT granted

RC = {REACHABLE: 0, UNREACHABLE: 3, INDETERMINATE: 4}

# curl exit codes that positively prove nothing is listening / nothing answered.
# Anything else (notably 35/60/77/8 = TLS or protocol trouble) means the endpoint may well be
# alive and we simply cannot talk to it -> indeterminate.
CURL_ABSENT_CODES = {
    6,   # couldn't resolve host
    7,   # failed to connect
    28,  # operation timed out
}


def resolve_base_url() -> Optional[str]:
    """Derive Central's HTTP base from the `agent-fleet` MCP endpoint.

    Deliberately NOT hardcoded: the MCP settings file is the single place the fleet host is
    configured, so a host change cannot leave this probe silently pointing at a dead URL.
    `$FLEET_CENTRAL_URL` overrides (tests / a split deployment).
    """
    env = os.environ.get("FLEET_CENTRAL_URL", "").strip()
    if env:
        return env.rstrip("/")
    for p in MCP_SETTINGS:
        try:
            if not p.is_file():
                continue
            d = json.loads(p.read_text())
            servers = d.get("mcpServers") or d
            for name, cfg in servers.items():
                if "fleet" not in name.lower():
                    continue
                url = (cfg or {}).get("url") or ""
                if not url:
                    continue
                # ".../mcp/" -> "..."; tolerate a missing trailing slash.
                return re.sub(r"/mcp/?$", "", url.strip()).rstrip("/")
        except Exception:  # noqa: BLE001 — config parsing is best-effort
            continue
    return None


def _curl_json(url: str) -> tuple:
    """GET via curl. Returns (obj, verdict, detail).

    curl (not urllib) because on macOS it links SecureTransport and therefore trusts the corp
    CA from the system keychain, which is not present in any on-disk pem bundle. See the module
    docstring — using urllib here silently inverts the whole guard.
    """
    if shutil.which("curl") is None:
        return None, INDETERMINATE, "curl not found on PATH (cannot verify — NOT proof of absence)"
    try:
        r = subprocess.run(  # noqa: S603,S607 — fixed argv, no shell
            ["curl", "-sS", "-m", str(TIMEOUT_SEC), "-w", "\n%{http_code}", url],
            capture_output=True, text=True, timeout=TIMEOUT_SEC + 4,
        )
    except subprocess.TimeoutExpired:
        return None, UNREACHABLE, f"curl wall-clock timeout after {TIMEOUT_SEC + 4}s"
    except Exception as e:  # noqa: BLE001
        return None, INDETERMINATE, f"could not run curl: {type(e).__name__}: {e}"

    if r.returncode != 0:
        verdict = UNREACHABLE if r.returncode in CURL_ABSENT_CODES else INDETERMINATE
        why = (r.stderr or "").strip().splitlines()
        return None, verdict, f"curl exit {r.returncode}: {why[-1] if why else 'no stderr'}"

    body, _, code = (r.stdout or "").rpartition("\n")
    code = code.strip()
    if code != "200":
        # Something answered, so the host is up — but not the API we expect. Not absence.
        return None, INDETERMINATE, f"HTTP {code or '???'} from {url}"
    try:
        return json.loads(body), REACHABLE, "ok"
    except Exception as e:  # noqa: BLE001
        return None, INDETERMINATE, f"HTTP 200 but unparsable JSON: {type(e).__name__}: {e}"


def _cache_read(base: str) -> Optional[bool]:
    """Read the cached positive for THIS base url.

    Keyed on the url on purpose: an unkeyed cache answered "reachable" for a probe pointed at a
    completely different endpoint (caught in testing — `FLEET_CENTRAL_URL=https://127.0.0.1:9`
    returned rc 0 from a warm cache). That would mask exactly the case where someone repoints
    Central and the new host is down.
    """
    try:
        d = json.loads(CACHE_FILE.read_text())
        if d.get("base") != base:
            return None
        if time.time() - float(d["at"]) <= REACHABLE_CACHE_TTL_SEC:
            return bool(d["ok"])
    except Exception:  # noqa: BLE001
        pass
    return None


def _cache_write(base: str, ok: bool) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps({"base": base, "ok": ok, "at": time.time()}))
    except Exception:  # noqa: BLE001 — cache is an optimisation, never load-bearing
        pass


def check_reachable(use_cache: bool = True) -> tuple:
    """(verdict, detail). verdict ∈ {reachable, unreachable, indeterminate}."""
    base = resolve_base_url()
    if not base:
        # No endpoint configured is NOT "the fleet is down" — it is a config gap we cannot
        # interpret, so it must not grant a NO-OP.
        return INDETERMINATE, "no agent-fleet endpoint in mcp.json (and no $FLEET_CENTRAL_URL)"
    if use_cache and _cache_read(base) is True:
        return REACHABLE, f"{base} (cached)"
    # A negative is deliberately NOT cached: an outage may have ended, and wrongly reporting
    # absence is precisely what licenses skipping the contract.
    obj, verdict, detail = _curl_json(f"{base}/api/agents")
    if verdict == REACHABLE and isinstance(obj, list):
        _cache_write(base, True)
        return REACHABLE, base
    if verdict == REACHABLE:
        return INDETERMINATE, f"{base}: unexpected payload from /api/agents"
    return verdict, f"{base}: {detail}"


def get_snapshot(task_id: str) -> tuple:
    """(snapshot_dict|None, verdict, detail) — counts of what ARRIVED in Central."""
    base = resolve_base_url()
    if not base:
        return None, INDETERMINATE, "no agent-fleet endpoint configured"
    ev, v1, d1 = _curl_json(f"{base}/api/tasks/{task_id}/events")
    ar, v2, d2 = _curl_json(f"{base}/api/artifacts/{task_id}")
    if v1 == REACHABLE or v2 == REACHABLE:
        return ({"events": len(ev) if isinstance(ev, list) else -1,
                 "artifacts": len(ar) if isinstance(ar, list) else -1},
                REACHABLE, base)
    # Neither call reached the API. Absence only when BOTH agree it is absent; a single
    # indeterminate keeps the whole result indeterminate (fail closed on the NO-OP grant).
    verdict = UNREACHABLE if (v1 == UNREACHABLE and v2 == UNREACHABLE) else INDETERMINATE
    return None, verdict, f"{base}: events={d1}; artifacts={d2}"


def cmd_reachable(args) -> int:
    verdict, detail = check_reachable(use_cache=not args.no_cache)
    if args.format == "json":
        print(json.dumps({"verdict": verdict, "detail": detail}))
    else:
        icon = {REACHABLE: "✅", UNREACHABLE: "⛔", INDETERMINATE: "❔"}[verdict]
        print(f"{icon} {verdict} — {detail}")
    return RC[verdict]


def cmd_snapshot(args) -> int:
    snap, verdict, detail = get_snapshot(args.task_id)
    if snap is None:
        print(json.dumps({"verdict": verdict, "detail": detail}))
        return RC[verdict]
    print(json.dumps({"verdict": verdict, **snap}))
    return 0


def cmd_delta(args) -> int:
    try:
        base = json.loads(args.baseline) if args.baseline else {}
        if not isinstance(base, dict):
            base = {}
    except Exception:  # noqa: BLE001 — an unparsable baseline must not be read as "grew"
        base = {}
    b_ev = int(base.get("events", 0) or 0)
    b_ar = int(base.get("artifacts", 0) or 0)
    snap, verdict, detail = get_snapshot(args.task_id)
    if snap is None:
        print(json.dumps({"verdict": verdict, "detail": detail}))
        return RC[verdict]
    grew = (snap["events"] > b_ev) or (snap["artifacts"] > b_ar)
    print(json.dumps({
        "verdict": verdict,
        "baseline": {"events": b_ev, "artifacts": b_ar},
        "now": {"events": snap["events"], "artifacts": snap["artifacts"]},
        "grew": grew,
    }))
    return 0 if grew else 2


# v1.49.83 — the write-side tools probe.
#
# Why it exists: the FLEET REPORTING RULE used to accept "I called it and got an error" as
# evidence that the fleet MCP was absent. Under Tool Search (`toolSearch.enabled=true`,
# `minPct=0`, `minTokens=0` on this host) every MCP tool schema is DEFERRED, so calling a tool
# the session has not loaded ALWAYS returns "tool not found". That made the rule's own
# evidentiary standard produce a false negative every single time, and a measured session
# force-acked 12 hooks on the strength of it.
#
# The server's advertised tool list settles it without depending on what the local session
# happens to have loaded. `POST /mcp/ {"method":"tools/list"}` needs no auth here and answers
# HTTP 200 with an SSE-framed JSON-RPC body.
WRITE_TOOLS = ("report_progress", "upload_artifact")


def _curl_post_json(url: str, payload: dict) -> tuple:
    """POST JSON via curl. Returns (obj, verdict, detail).

    curl rather than urllib for the same reason as `_curl_json` — on this host urllib cannot
    verify the corp CA (no usable bundle on disk), so using it here would silently invert the
    guard into "always indeterminate". Accepts the SSE framing FastMCP replies with
    (`event: message` / `data: {...}`) as well as a bare JSON body.
    """
    if shutil.which("curl") is None:
        return None, INDETERMINATE, "curl not found on PATH (cannot verify — NOT proof of absence)"
    try:
        r = subprocess.run(
            ["curl", "-sS", "-m", str(TIMEOUT_SEC), "-X", "POST", url,
             "-H", "Content-Type: application/json",
             "-H", "Accept: application/json, text/event-stream",
             "-d", json.dumps(payload), "-w", "\n%{http_code}"],
            capture_output=True, text=True, timeout=TIMEOUT_SEC + 4)
    except subprocess.TimeoutExpired:
        return None, UNREACHABLE, f"curl wall-clock timeout after {TIMEOUT_SEC + 4}s"
    except Exception as e:  # noqa: BLE001
        return None, INDETERMINATE, f"could not run curl: {type(e).__name__}: {e}"

    if r.returncode != 0:
        why = [l for l in (r.stderr or "").splitlines() if l.strip()]
        verdict = UNREACHABLE if r.returncode in CURL_ABSENT_CODES else INDETERMINATE
        return None, verdict, f"curl exit {r.returncode}: {why[-1] if why else 'no stderr'}"

    body, _, code = (r.stdout or "").rpartition("\n")
    code = code.strip()
    if code != "200":
        return None, INDETERMINATE, f"HTTP {code} from {url} (not proof of absence)"

    # SSE framing: pick the last `data:` line. A bare JSON body is handled by the fallback.
    obj = None
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            try:
                obj = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
    if obj is None:
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            return None, INDETERMINATE, "reply was neither SSE-framed nor bare JSON"
    return obj, REACHABLE, f"HTTP 200 from {url}"


def probe_tools() -> tuple:
    """Does the fleet MCP server ADVERTISE the write-side tools? Returns (verdict, detail).

    Verdicts carry the same meaning as everywhere else in this file, so callers branch on one
    vocabulary:
      reachable      -> the server answered AND advertises all of WRITE_TOOLS
      unreachable    -> the server answered and does NOT advertise them (real absence)
      indeterminate  -> could not look (no endpoint configured / TLS / non-200 / bad body)

    ⚠️ `unreachable` here means "the tool is genuinely not offered", NOT "the host is down" —
    a down host also lands here via curl's absence codes. Either way a NO-OP is legitimate,
    which is the only distinction the caller needs.
    """
    base = resolve_base_url()
    if not base:
        return INDETERMINATE, "no agent-fleet MCP endpoint configured (cannot verify)"
    url = f"{base}/mcp/"
    obj, verdict, detail = _curl_post_json(
        url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    if verdict != REACHABLE:
        return verdict, detail

    tools = (((obj or {}).get("result") or {}).get("tools")) or []
    names = {t.get("name") for t in tools if isinstance(t, dict)}
    if not names:
        return INDETERMINATE, f"{detail}; tools/list returned no names (cannot verify)"
    missing = [t for t in WRITE_TOOLS if t not in names]
    if missing:
        return UNREACHABLE, (f"server advertises {len(names)} tools but NOT "
                             f"{', '.join(missing)} — absence is real")
    return REACHABLE, (f"server advertises all write tools ({', '.join(WRITE_TOOLS)}) "
                       f"among {len(names)}")


def cmd_tools(args) -> int:
    """`fleet_probe.py tools` — 0 advertised / 3 genuinely absent / 4 could not look."""
    verdict, detail = probe_tools()
    if getattr(args, "format", "text") == "json":
        print(json.dumps({"verdict": verdict, "detail": detail,
                          "write_tools": list(WRITE_TOOLS)}))
    else:
        icon = {REACHABLE: "✅", UNREACHABLE: "🚫", INDETERMINATE: "❔"}[verdict]
        print(f"{icon} fleet write tools: {verdict} — {detail}")
        if verdict == REACHABLE:
            print("   A --noop reason claiming these tools are missing is FALSE. Load them "
                  "first:  tool_search(tool_id=\"agent-fleet::report_progress\")")
    return RC[verdict]


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("reachable", help="0=reachable 3=unreachable 4=indeterminate")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.add_argument("--no-cache", action="store_true")
    p.set_defaults(fn=cmd_reachable)

    p = sub.add_parser(
        "tools",
        help="v1.49.83: does the server ADVERTISE report_progress/upload_artifact? "
             "(0=yes 3=genuinely absent 4=could not look)")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(fn=cmd_tools)

    p = sub.add_parser("snapshot", help="arrived event/artifact counts (0 / 3 / 4)")
    p.add_argument("--task-id", required=True)
    p.set_defaults(fn=cmd_snapshot)

    p = sub.add_parser("delta", help="0=grew 2=no growth 3=unreachable 4=indeterminate")
    p.add_argument("--task-id", required=True)
    p.add_argument("--baseline", default="", help='JSON, e.g. {"events":3,"artifacts":1}')
    p.set_defaults(fn=cmd_delta)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Analyze ship-check run metrics from kiro-cli session data.

Reads ~/.kiro/sessions/cli/{uuid}.json and computes turn / call / credit /
context-usage stats for a window starting at --since.

Usage:
    analyze-run-metrics.py --since "2026-06-12T14:53:00+08:00"
    analyze-run-metrics.py --since ... --mode plan-doc
    analyze-run-metrics.py --since ... --session-uuid 7150de26-5fb6-4c04-8769-636bc7d41952

Auto-detect order for current session UUID:
    1. --session-uuid arg
    2. $KIRO_SESSION_ID env var (set by kiro-cli)
    3. Most-recently-updated session.json matching $PWD
    4. Most-recently-updated session.json (any cwd)

Output:
    Markdown to stdout. --mode summary (default) is verbose for ship-check
    Close Stage console; --mode plan-doc is concise for appending to plan doc.

Exit codes:
    0  success
    1  could not detect session UUID
    2  no turns found in window
    3  could not read session.json
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

SESSIONS_DIR = Path.home() / ".kiro" / "sessions" / "cli"


def parse_iso(s: str) -> float:
    """Parse ISO-8601 timestamp to Unix seconds."""
    if not s:
        return 0.0
    s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s).timestamp()


def fmt_duration(seconds: float) -> str:
    """Human-readable duration."""
    if seconds < 1:
        return "<1s"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m}m"


def find_session_uuid(cwd_match: str = None) -> str:
    """Find current session UUID via env var or filesystem heuristics."""
    if "KIRO_SESSION_ID" in os.environ:
        return os.environ["KIRO_SESSION_ID"]

    candidates_match = []
    candidates_any = []
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            with open(f) as fp:
                data = json.load(fp)
            updated = data.get("updated_at", "")
            cwd = data.get("cwd", "")
            entry = (updated, f.stem)
            if cwd_match and cwd == cwd_match:
                candidates_match.append(entry)
            candidates_any.append(entry)
        except Exception:
            continue

    candidates_match.sort(reverse=True)
    candidates_any.sort(reverse=True)
    if candidates_match:
        return candidates_match[0][1]
    if candidates_any:
        return candidates_any[0][1]
    return None


def extract_metrics(session_uuid: str, since_ts: float):
    """Read session.json and compute metrics for turns >= since_ts."""
    json_path = SESSIONS_DIR / f"{session_uuid}.json"
    if not json_path.exists():
        return None
    try:
        with open(json_path) as f:
            data = json.load(f)
    except Exception:
        return None

    cm = data.get("session_state", {}).get("conversation_metadata", {})
    turns = cm.get("user_turn_metadatas", []) or []

    relevant = []
    for t in turns:
        result = t.get("result", {}) or {}
        ok = result.get("Ok") or result.get("Err")
        if not ok or not isinstance(ok, dict):
            continue
        meta = ok.get("meta", {}) or {}
        ts = meta.get("timestamp") or 0
        if ts >= since_ts:
            relevant.append({
                "ts": ts,
                "ctx_pct": t.get("context_usage_percentage") or 0,
                "metering": t.get("metering_usage") or [],
            })

    if not relevant:
        return None

    run_credits = sum(sum(m.get("value", 0) for m in t["metering"]) for t in relevant)
    run_calls = sum(len(t["metering"]) for t in relevant)
    peak_ctx = max((t["ctx_pct"] for t in relevant), default=0)
    duration_s = relevant[-1]["ts"] - relevant[0]["ts"] if len(relevant) > 1 else 0

    session_credits = sum(
        sum(m.get("value", 0) for m in (t.get("metering_usage") or []))
        for t in turns
    )
    session_calls = sum(len(t.get("metering_usage") or []) for t in turns)
    session_turns = sum(
        1 for t in turns
        if (t.get("result", {}).get("Ok") or t.get("result", {}).get("Err"))
    )

    return {
        "uuid": session_uuid,
        "run_turns": len(relevant),
        "run_calls": run_calls,
        "run_credits": run_credits,
        "run_duration_s": duration_s,
        "run_peak_ctx_pct": peak_ctx,
        "session_turns": session_turns,
        "session_calls": session_calls,
        "session_credits": session_credits,
    }


def render_summary(m: dict) -> str:
    """Verbose markdown for ship-check Close Stage console output."""
    n = max(m["run_turns"], 1)
    return (
        "📊 Run metrics:\n"
        f"- Duration: {fmt_duration(m['run_duration_s'])}\n"
        f"- Turns: {m['run_turns']} / Calls: {m['run_calls']} / "
        f"Credits: {m['run_credits']:.2f}\n"
        f"- Peak context: {m['run_peak_ctx_pct']:.1f}%\n"
        f"- Avg per turn: {m['run_credits'] / n:.2f} credits / "
        f"{m['run_calls'] / n:.1f} calls\n"
        f"- Session: {m['uuid'][:8]} "
        f"(cumulative {m['session_credits']:.2f} credits / "
        f"{m['session_turns']} turns)"
    )


def render_for_plan_doc(m: dict) -> str:
    """Concise markdown to append as a `## Metrics` section in plan doc."""
    return (
        "\n## Metrics\n"
        f"- Duration: {fmt_duration(m['run_duration_s'])}\n"
        f"- Turns: {m['run_turns']} / Calls: {m['run_calls']} / "
        f"Credits: {m['run_credits']:.2f}\n"
        f"- Peak context: {m['run_peak_ctx_pct']:.1f}%\n"
        f"- Session: {m['uuid'][:8]} "
        f"(cumulative {m['session_credits']:.2f} credits)"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True,
                        help="ISO-8601 timestamp marking ship-check start")
    parser.add_argument("--session-uuid",
                        help="Override session UUID; default uses $KIRO_SESSION_ID")
    parser.add_argument("--workspace",
                        help="Session workspace used to disambiguate the session by recorded "
                             "cwd. v1.49.65: pass this explicitly under worktree_isolation — "
                             "the agent's cwd stays in the main tree while the session's real "
                             "workspace is a per-uuid worktree, so a bare os.getcwd() matches "
                             "the wrong tree (or nothing). Defaults to cwd.")
    parser.add_argument("--mode", choices=["summary", "plan-doc"],
                        default="summary",
                        help="Output style: summary for console, plan-doc for "
                             "appending to plan doc")
    args = parser.parse_args()

    # v1.49.65: prefer the explicitly-passed workspace over cwd (see --workspace help).
    _cwd_match = args.workspace or os.getcwd()
    if not args.workspace and os.environ.get("SHIPCHECK_WORKTREE_ISOLATION") == "1":
        sys.stderr.write(
            f"⚠️ analyze-run-metrics: no --workspace given → matching session by cwd "
            f"{_cwd_match} while worktree_isolation is ON (may be the wrong tree)\n"
        )
    uuid = args.session_uuid or find_session_uuid(cwd_match=_cwd_match)
    if not uuid:
        sys.stderr.write(
            "⚠️ Could not detect current session UUID. "
            "Set $KIRO_SESSION_ID or pass --session-uuid.\n"
        )
        sys.exit(1)

    try:
        since_ts = parse_iso(args.since)
    except Exception as e:
        sys.stderr.write(f"⚠️ Invalid --since timestamp: {e}\n")
        sys.exit(1)

    m = extract_metrics(uuid, since_ts)
    if m is None:
        json_path = SESSIONS_DIR / f"{uuid}.json"
        if not json_path.exists():
            sys.stderr.write(
                f"⚠️ Session file not found: {json_path}\n"
            )
            sys.exit(3)
        sys.stderr.write(
            f"⚠️ No turns in session {uuid[:8]} since {args.since}\n"
        )
        sys.exit(2)

    out = render_summary(m) if args.mode == "summary" else render_for_plan_doc(m)
    print(out)


if __name__ == "__main__":
    main()

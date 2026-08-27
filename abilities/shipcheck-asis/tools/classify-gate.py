#!/usr/bin/env python3
"""
classify-gate.py — Decision Gate B1-B5 / W1-W6 pure classification.

Design (v1.42.1+):
- Consumes JSON output of run-assertions.py (blast-radius verdicts A1-A13).
- Takes additional "diff facts" flags via CLI (build result / multi-package / IAM signal / etc.)
- Implements references/decision-gate-rules.md pseudocode as a pure function.

Scope (per plan `verification-layer-assertion-gate-executors`):
  ✅ auto: B2 (= A1 fail), B3 (= A4 fail), B4 (= A2 unexpected_source),
           B5 (any A* fail), W1 (auto-gen extras), W3 (multi-package),
           W5 (IAM/CDK/permission regex on changed paths), W6 (≥3 W upgrade)
  🔵 input: B1 (build result), W2 (out-of-scope same-pattern bug),
            W4 (README/CHANGELOG heuristic)
  ❌ NOT in scope: MG1/MG2/MG3 Merge Gate — those are USER 🚦 gate, not classifier.

Usage:
  classify-gate.py --assertions <run-assertions.json>
                   [--build-status pass|fail|skipped]
                   [--multi-package]
                   [--out-of-scope-bug <msg>]
                   [--readme-changelog-suggested]
                   [--changed-paths <path.txt>]      # newline-delimited (fallback to assertions.change_set.files)
                   [--format md|json]
                   [--output <file>]

Exit codes:
  0  = PASS (auto-proceed to Execution Layer)
  1  = WARNING (user must ack)
  2  = BLOCKED (STOP + fix)
  3  = invocation error
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ConditionHit:
    id: str  # B1..B5 / W1..W6
    category: str  # "blocker" | "warning"
    reason: str
    action: str
    source: str = "unknown"  # "assertions" | "input" | "derived"
    evidence: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Loading assertions JSON
# ---------------------------------------------------------------------------


def load_assertions(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"assertions JSON not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("schema_version") != "1.0":
        raise ValueError(
            f"unsupported assertions schema_version: {data.get('schema_version')!r} (expected 1.0)"
        )
    if "assertions" not in data:
        raise ValueError("assertions JSON missing 'assertions' key")
    return data


def get_assertion(data: dict[str, Any], assertion_id: str) -> Optional[dict[str, Any]]:
    for a in data.get("assertions", []):
        if a.get("assertion_id") == assertion_id:
            return a
    return None


def any_assertion_failed(data: dict[str, Any]) -> tuple[bool, list[str]]:
    failed: list[str] = []
    for a in data.get("assertions", []):
        if a.get("triggered") and a.get("verdict") == "fail":
            failed.append(a.get("assertion_id", "?"))
    return (bool(failed), failed)


# ---------------------------------------------------------------------------
# IAM / permission regex for W5
# ---------------------------------------------------------------------------

IAM_PATH_RE = re.compile(
    r"(?i)(iam|role|policy|permission|cdk|cloudformation|resource_based_policy|assume_role|"
    r"trust_policy|principal|assumeRole|\.tf$|\.hcl$)"
)


def _extract_changed_paths(
    assertions_data: dict[str, Any],
    override_paths: list[str],
) -> list[str]:
    if override_paths:
        return override_paths
    change_set = assertions_data.get("change_set") or {}
    files = change_set.get("files") or []
    # Also merge in A2 evidence if present (actual diff scope)
    a2 = get_assertion(assertions_data, "A2")
    if a2 and a2.get("evidence"):
        ev = a2["evidence"]
        for key in ("actual", "planned", "unexpected_source", "unexpected_autogen"):
            files.extend(ev.get(key) or [])
    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            result.append(f)
    return result


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(
    assertions_data: dict[str, Any],
    build_status: str = "unknown",
    multi_package: bool = False,
    out_of_scope_bug: Optional[str] = None,
    readme_changelog_suggested: bool = False,
    changed_paths_override: Optional[list[str]] = None,
) -> tuple[list[ConditionHit], list[ConditionHit]]:
    """
    Returns (blockers, warnings). W6 auto-appended if len(warnings) >= 3.
    """
    blockers: list[ConditionHit] = []
    warnings: list[ConditionHit] = []

    # ---- B1: Build failed ----
    if build_status == "fail":
        blockers.append(
            ConditionHit(
                id="B1",
                category="blocker",
                reason="Build failed (code cannot compile/package)",
                action="Fix build errors and re-run Verification Layer",
                source="input",
                evidence={"build_status": build_status},
            )
        )
    # ---- B2: A1 failed → old values still exist ----
    a1 = get_assertion(assertions_data, "A1")
    if a1 and a1.get("triggered") and a1.get("verdict") == "fail":
        blockers.append(
            ConditionHit(
                id="B2",
                category="blocker",
                reason="A1 failed — old values still present in files that should have been updated",
                action="grep the old value, fix remaining references, re-run [D10]",
                source="assertions",
                evidence={"a1_detail": a1.get("detail"), "a1_evidence": a1.get("evidence")},
            )
        )
    # ---- B3: A4 failed → new symbol/path does not exist ----
    a4 = get_assertion(assertions_data, "A4")
    if a4 and a4.get("triggered") and a4.get("verdict") == "fail":
        blockers.append(
            ConditionHit(
                id="B3",
                category="blocker",
                reason="A4 failed — new path/symbol reference does not exist (ls/import failure)",
                action="Verify the referenced path/module exists or fix the reference",
                source="assertions",
                evidence={"a4_detail": a4.get("detail"), "a4_evidence": a4.get("evidence")},
            )
        )
    # ---- B4: A2 has unexpected_source (scope drift, non-auto-generated) ----
    a2 = get_assertion(assertions_data, "A2")
    if a2 and a2.get("triggered") and a2.get("verdict") == "fail":
        ev = a2.get("evidence") or {}
        unexpected_source = ev.get("unexpected_source") or []
        if unexpected_source:
            blockers.append(
                ConditionHit(
                    id="B4",
                    category="blocker",
                    reason="A2 failed — diff contains source files not in plan (scope drift)",
                    action="Revert unplanned files or update plan doc + re-confirm at [C20]",
                    source="assertions",
                    evidence={"unexpected_source": unexpected_source, "a2_detail": a2.get("detail")},
                )
            )
    # ---- B5: any triggered auto assertion failed (any-fail cascade) ----
    #    Skip A1/A2/A4 since they already trigger B2/B4/B3 individually.
    covered_ids = {"A1", "A2", "A4"}
    _, failed_ids = any_assertion_failed(assertions_data)
    residual_fails = [f for f in failed_ids if f.split("(")[0] not in covered_ids]
    if residual_fails:
        blockers.append(
            ConditionHit(
                id="B5",
                category="blocker",
                reason=f"Automated assertion(s) failed: {', '.join(residual_fails)}",
                action="Read the [D10] assertion details, fix, and re-run",
                source="assertions",
                evidence={"failed_assertion_ids": residual_fails},
            )
        )

    # ---- W1: A2 has unexpected_autogen extras (auto-generated files) ----
    if a2 and a2.get("evidence"):
        autogen_extras = a2["evidence"].get("unexpected_autogen") or a2["evidence"].get("auto_gen_extras") or []
        if autogen_extras:
            warnings.append(
                ConditionHit(
                    id="W1",
                    category="warning",
                    reason=f"{len(autogen_extras)} auto-generated file(s) not in plan — verify build regenerated as expected",
                    action="Confirm build output is intentional",
                    source="assertions",
                    evidence={"auto_gen_extras": autogen_extras},
                )
            )
    # ---- W2: out-of-scope same-pattern bug (input flag) ----
    if out_of_scope_bug:
        warnings.append(
            ConditionHit(
                id="W2",
                category="warning",
                reason=f"Out-of-scope same-pattern bug detected: {out_of_scope_bug}",
                action="Decide whether to fix in this CR or open a follow-up",
                source="input",
                evidence={"description": out_of_scope_bug},
            )
        )
    # ---- W3: multi-package change ----
    if multi_package:
        warnings.append(
            ConditionHit(
                id="W3",
                category="warning",
                reason="Multi-package change detected — may need separate CRs per package",
                action="Verify with reviewer whether cross-package change is intended for a single CR",
                source="input",
                evidence={"multi_package": True},
            )
        )
    # ---- W4: README/CHANGELOG maybe needs update (input flag) ----
    if readme_changelog_suggested:
        warnings.append(
            ConditionHit(
                id="W4",
                category="warning",
                reason="README/CHANGELOG may need updates but no such file changed",
                action="Consider adding a CHANGELOG entry (advisory only)",
                source="input",
                evidence={"readme_changelog_suggested": True},
            )
        )
    # ---- W5: IAM/CDK/permission touched (regex over changed paths) ----
    changed_paths = _extract_changed_paths(assertions_data, changed_paths_override or [])
    iam_hits = [p for p in changed_paths if IAM_PATH_RE.search(p)]
    if iam_hits:
        warnings.append(
            ConditionHit(
                id="W5",
                category="warning",
                reason=f"{len(iam_hits)} path(s) touch IAM/CDK/permission surface",
                action="Manual review of permission delta strongly recommended",
                source="derived",
                evidence={"iam_paths": iam_hits[:20]},
            )
        )

    # ---- W6: 3+ warnings → escalate ----
    if len(warnings) >= 3:
        warnings.append(
            ConditionHit(
                id="W6",
                category="warning",
                reason=f"{len(warnings)} warnings accumulated — risk escalation, MUST stop for user confirm",
                action="Show all warnings to user and require Y/modify/cancel",
                source="derived",
                evidence={"warning_ids": [w.id for w in warnings]},
            )
        )

    return blockers, warnings


def verdict_from(blockers: list[ConditionHit], warnings: list[ConditionHit]) -> str:
    if blockers:
        return "BLOCKED"
    if warnings:
        return "WARNING"
    return "PASS"


# ---------------------------------------------------------------------------
# Manual review counting (A6 / A10(b) / A12(b) / A12(c) / A13(a) / A13(b))
# ---------------------------------------------------------------------------


def count_manual_items(data: dict[str, Any]) -> list[str]:
    manuals: list[str] = []
    for a in data.get("assertions", []):
        if a.get("triggered") and a.get("verdict") == "manual":
            manuals.append(a.get("assertion_id", "?"))
    return manuals


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def emit_md(
    verdict: str,
    blockers: list[ConditionHit],
    warnings: list[ConditionHit],
    manuals: list[str],
) -> str:
    lines: list[str] = ["### Decision gate Stage classification", ""]
    verdict_map = {"BLOCKED": "❌ BLOCKED", "WARNING": "⚠️ WARNING", "PASS": "✅ PASS"}
    lines.append(f"**Verdict**: {verdict_map.get(verdict, verdict)}")
    lines.append("")
    if blockers:
        lines.append("**Blockers:**")
        lines.append("| ID | Reason | Action | Source |")
        lines.append("|----|--------|--------|--------|")
        for b in blockers:
            lines.append(f"| {b.id} | {b.reason} | {b.action} | {b.source} |")
        lines.append("")
    if warnings:
        lines.append("**Warnings:**")
        lines.append("| ID | Reason | Action | Source |")
        lines.append("|----|--------|--------|--------|")
        for w in warnings:
            lines.append(f"| {w.id} | {w.reason} | {w.action} | {w.source} |")
        lines.append("")
    if manuals:
        lines.append(f"**Manual review items** (not classifier-decidable): {', '.join(manuals)}")
        lines.append("- Verdict does NOT include these — agent must judge separately")
        lines.append("")
    if verdict == "PASS" and not manuals:
        lines.append("→ Auto-proceed to Execution Layer / Commit prep Stage")
    elif verdict == "PASS" and manuals:
        lines.append("→ No auto B/W hits; agent MUST still review manual items before proceeding")
    elif verdict == "WARNING":
        lines.append("→ Ask user: 继续 / 修改 / 取消？")
    else:
        lines.append("→ STOP; fix blockers and re-run Blast radius Stage")
    return "\n".join(lines)


def emit_json(
    verdict: str,
    blockers: list[ConditionHit],
    warnings: list[ConditionHit],
    manuals: list[str],
    input_summary: dict[str, Any],
) -> str:
    payload = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "blockers": [asdict(b) for b in blockers],
        "warnings": [asdict(w) for w in warnings],
        "manual_review_items": manuals,
        "input_summary": input_summary,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Decision Gate B1-B5 / W1-W6 pure classification.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--assertions", required=True, help="Path to run-assertions.py JSON output")
    parser.add_argument(
        "--build-status",
        choices=["pass", "fail", "skipped", "unknown"],
        default="unknown",
        help="Build result flag (drives B1)",
    )
    parser.add_argument("--multi-package", action="store_true", help="Diff spans multiple packages (drives W3)")
    parser.add_argument("--out-of-scope-bug", default=None, help="Same-pattern bug outside scope (drives W2)")
    parser.add_argument(
        "--readme-changelog-suggested",
        action="store_true",
        help="README/CHANGELOG update recommended but not applied (drives W4)",
    )
    parser.add_argument(
        "--changed-paths",
        default=None,
        help="Path to a newline-delimited file of changed paths (override; else use assertions.change_set.files + A2 evidence)",
    )
    parser.add_argument("--format", choices=["md", "json"], default="md", help="Output format (default: md)")
    parser.add_argument("--output", default=None, help="Write output to file (default: stdout)")
    args = parser.parse_args(argv)

    assertions_path = Path(args.assertions).expanduser().resolve()
    try:
        data = load_assertions(assertions_path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 3

    changed_paths_override: Optional[list[str]] = None
    if args.changed_paths:
        try:
            cp = Path(args.changed_paths).expanduser().resolve()
            changed_paths_override = [line.strip() for line in cp.read_text(encoding="utf-8").splitlines() if line.strip()]
        except OSError as e:
            print(f"error reading --changed-paths: {e}", file=sys.stderr)
            return 3

    blockers, warnings = classify(
        data,
        build_status=args.build_status,
        multi_package=args.multi_package,
        out_of_scope_bug=args.out_of_scope_bug,
        readme_changelog_suggested=args.readme_changelog_suggested,
        changed_paths_override=changed_paths_override,
    )
    verdict = verdict_from(blockers, warnings)
    manuals = count_manual_items(data)

    if args.format == "md":
        output = emit_md(verdict, blockers, warnings, manuals)
    else:
        input_summary = {
            "assertions_path": str(assertions_path),
            "build_status": args.build_status,
            "multi_package": args.multi_package,
            "out_of_scope_bug": args.out_of_scope_bug,
            "readme_changelog_suggested": args.readme_changelog_suggested,
        }
        output = emit_json(verdict, blockers, warnings, manuals, input_summary)

    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    else:
        print(output)

    if verdict == "BLOCKED":
        return 2
    if verdict == "WARNING":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

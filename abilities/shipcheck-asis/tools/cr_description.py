#!/usr/bin/env python3
"""cr_description.py — deterministic CR description rendering + exit-code lint.

WHY (v1.50.0)
-------------
[E02] "Generate CR description" was one line of prose with no template, schema or
checker, so the shape and depth of every CR description was whatever the agent
improvised at that moment. Two measured costs: one ship spent 6 edit+measure rounds
(5032 -> 3973 chars, ~12 tool calls) converging on the 4000-char ceiling, and several
sessions shipped `?????` because `cr` under a non-UTF-8 locale mangles non-ASCII.

Prose MANDATORY does not bind an agent under context pressure (anti-pattern
`anti-prose-mandatory-cannot-bind-agent`), so this splits the document in two:

  machine sections        Testing + Meta, rendered from the state DB / git / plan doc.
                          Zero LLM participation, therefore stable across ships.
  agent-authored sections What / Why / How, still written by the agent but constrained
                          by the spec (presence, char quota, no placeholders, ASCII).

and enforces the contract with an exit code that [E07] must respect:

    lint  ->  0 ok / 1 a check failed / 2 spec or input unusable

USAGE
-----
    render --uuid <shipcheck_uuid> [--workspace P] [--plan-doc P] [--task-url U]
           [--section-file what=path.md ...] [--out path.md]
    lint   --uuid <shipcheck_uuid> --file <assembled description.md>

The Testing section answers "did this change go through automated verification, and how
solid was that verification" — NOT coverage percentage. Its core output is a set of
deterministically computed strength caveats, so "how solid" is never the agent grading
its own work.

SPEC:  config/cr-description-spec.yaml   (single source of truth for sections/quotas)
GUIDE: references/[execution]cr-description-template.md
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

SKILL_DIR = Path(__file__).resolve().parents[1]
SPEC_PATH = SKILL_DIR / "config" / "cr-description-spec.yaml"

sys.path.insert(0, str(SKILL_DIR / "scripts"))

RC_OK = 0
RC_CHECK_FAILED = 1
RC_UNUSABLE = 2


# --------------------------------------------------------------------------- spec


def load_spec(spec_path: Path = SPEC_PATH) -> dict:
    """Load + minimally validate the spec. Raises ValueError on anything unusable."""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment issue, surfaced as rc 2
        raise ValueError(f"PyYAML unavailable: {exc}") from exc
    if not spec_path.exists():
        raise ValueError(f"spec not found: {spec_path}")
    try:
        spec = yaml.safe_load(spec_path.read_text())
    except Exception as exc:
        raise ValueError(f"spec is not valid YAML: {exc}") from exc
    if not isinstance(spec, dict):
        raise ValueError("spec root must be a mapping")
    for key in ("version", "total_limit", "sections", "checks"):
        if key not in spec:
            raise ValueError(f"spec missing required key: {key}")
    if not isinstance(spec["sections"], list) or not spec["sections"]:
        raise ValueError("spec.sections must be a non-empty list")
    seen = set()
    for s in spec["sections"]:
        for key in ("id", "order", "heading", "kind", "required", "char_quota", "checks"):
            if key not in s:
                raise ValueError(f"section {s.get('id', '?')!r} missing key: {key}")
        if s["kind"] not in ("machine", "agent-authored"):
            raise ValueError(f"section {s['id']!r}: kind must be machine|agent-authored")
        if s["id"] in seen:
            raise ValueError(f"duplicate section id: {s['id']}")
        seen.add(s["id"])
        for c in s["checks"]:
            if c not in spec["checks"]:
                raise ValueError(f"section {s['id']!r} references undefined check {c!r}")
    quota_sum = sum(s["char_quota"] for s in spec["sections"])
    if quota_sum > spec["total_limit"]:
        raise ValueError(f"quota sum {quota_sum} exceeds total_limit {spec['total_limit']}")
    return spec


def ordered_sections(spec: dict) -> list:
    return sorted(spec["sections"], key=lambda s: s["order"])


# --------------------------------------------------------------------------- checks


def _check_non_empty(body: str, _sec: dict, _spec: dict) -> Optional[str]:
    return None if body.strip() else "section is empty"


def _check_no_placeholder(body: str, _sec: dict, spec: dict) -> Optional[str]:
    pats = spec["checks"]["no_placeholder"].get("patterns") or []
    low = body.lower()
    hits = [p for p in pats if p.lower() in low]
    return f"unfilled placeholder(s): {', '.join(hits)}" if hits else None


def _check_ascii_only(body: str, _sec: dict, _spec: dict) -> Optional[str]:
    bad = sorted({ch for ch in body if ord(ch) > 127})
    if not bad:
        return None
    shown = " ".join(f"{ch!r}(U+{ord(ch):04X})" for ch in bad[:6])
    return (f"{len(bad)} non-ASCII char(s): {shown} — `cr` under a non-UTF-8 locale "
            f"rewrites these to '?' (see anti-cr-nonascii-mangled-by-c-locale)")


def _check_within_quota(body: str, sec: dict, _spec: dict) -> Optional[str]:
    n = len(body)
    return None if n <= sec["char_quota"] else f"{n} chars exceeds quota {sec['char_quota']}"


CHECKERS = {
    "non_empty": _check_non_empty,
    "no_placeholder": _check_no_placeholder,
    "ascii_only": _check_ascii_only,
    "within_quota": _check_within_quota,
}


# --------------------------------------------------------------- testing section


def _fmt_duration(ms) -> str:
    if ms in (None, ""):
        return "-"
    try:
        return f"{int(ms) / 1000:.1f}s"
    except (TypeError, ValueError):
        return "-"


RESULT_LABEL = {
    "pass": "PASS",
    "fail": "FAIL",
    "skip": "SKIP",
    "dedup": "DEDUP",
    "degraded-pass": "PASS (degraded)",
    "degraded-fail": "FAIL (degraded)",
}


def compute_caveats(evidence: dict) -> list:
    """Deterministically derive the strength caveats from recorded evidence.

    Every branch reads a field that already exists (verdict / source / severity /
    result / framework_eligible / assertions_verdict) — nothing here is a judgement the
    agent could shade in its own favour, which is the whole point of the section.
    """
    meta = evidence.get("meta") or {}
    execs = evidence.get("executions") or []
    out = []

    if not execs and not meta:
        out.append("no structured test record (pre-v1.50 session) - verdict shown is "
                   "unverified by this renderer")
        return out

    verdict = (meta.get("verdict") or "").strip()
    if verdict == "exempt":
        out.append("no framework test ran (exempt) - only alternative verification")

    if (meta.get("source") or "").strip() == "plan-hand-written":
        out.append("test list was hand-written for this change, not declared by the package")

    n_skipped = sum(1 for e in execs
                    if e.get("severity") == "must-run" and e.get("result") == "skip")
    if n_skipped:
        out.append(f"{n_skipped} must-run execution(s) skipped by skip_conditions")

    n_degraded = sum(1 for e in execs
                     if e.get("result") in ("degraded-pass", "degraded-fail"))
    if n_degraded:
        out.append(f"{n_degraded} execution(s) ran a degraded fallback path")

    if meta.get("framework_eligible") and verdict == "exempt":
        out.append("package is framework_eligible but still exempt - a real test suite is owed")

    av = (meta.get("assertions_verdict") or "")
    if "environment" in av.lower():
        out.append("some blast-radius assertions did not pass and were attributed to "
                   "the environment")

    n_failed = sum(1 for e in execs if e.get("result") in ("fail", "degraded-fail"))
    if n_failed:
        out.append(f"{n_failed} execution(s) ended in FAIL")

    return out


def render_testing(evidence: dict, spec: dict, collapse: bool = False) -> str:
    """Render the Testing section body (no heading) from recorded evidence."""
    meta = evidence.get("meta") or {}
    execs = evidence.get("executions") or []
    pointer = spec.get("testing_render", {}).get("analyzer_pointer", "")
    lines = []

    if not execs and not meta:
        lines.append("Verdict: unknown - no structured test record for this session "
                     "(pre-v1.50 session, or [D05] did not record).")
    else:
        verdict = meta.get("verdict") or "unknown"
        strategy = meta.get("strategy") or "unknown"
        lines.append(f"Verdict: {verdict}   Strategy: {strategy}")
        src = meta.get("source")
        if src:
            note = " <- package-declared, authoritative" if src == "in-repo" else ""
            lines.append(f"Test-list source: {src}{note}")

    if execs:
        if collapse:
            counts: dict = {}
            for e in execs:
                counts[e["result"]] = counts.get(e["result"], 0) + 1
            summary = ", ".join(f"{RESULT_LABEL.get(k, k)}={v}" for k, v in sorted(counts.items()))
            lines.append("")
            lines.append(f"Executions ({len(execs)}): {summary}   [table collapsed to fit]")
        else:
            lines.append("")
            lines.append("| # | id | kind | tier | sev | result | time |")
            lines.append("|---|----|------|------|-----|--------|------|")
            for i, e in enumerate(execs, 1):
                res = RESULT_LABEL.get(e.get("result"), e.get("result") or "?")
                detail = (e.get("detail") or "").strip()
                if detail:
                    res = f"{res} ({detail})"
                if e.get("result") == "skip" and e.get("skip_reason"):
                    res = f"{res} [{e['skip_reason']}]"
                lines.append(
                    f"| {i} | {e.get('exec_id', '?')} | {e.get('kind', '?')} "
                    f"| {e.get('tier') or 'unit'} | {e.get('severity') or '-'} "
                    f"| {res} | {_fmt_duration(e.get('duration_ms'))} |"
                )

    extras = []
    if meta.get("assertions_verdict"):
        extras.append(f"Blast radius: {meta['assertions_verdict']}")
    if meta.get("anchors_hit"):
        extras.append(f"Plan anchors: {meta['anchors_hit']} critical_paths hit")
    if extras:
        lines.append("")
        lines.extend(extras)

    caveats = compute_caveats(evidence)
    lines.append("")
    if caveats:
        lines.append("Strength caveats (auto-detected):")
        lines.extend(f"  - {c}" for c in caveats)
    else:
        lines.append("Strength caveats (auto-detected): none")

    if pointer:
        lines.append("")
        lines.append(pointer)
    return "\n".join(lines)


# ------------------------------------------------------------------ meta section


def _git(workspace: Optional[str], *args: str) -> str:
    if not workspace:
        return ""
    try:
        r = subprocess.run(["git", *args], cwd=workspace, capture_output=True,
                           text=True, timeout=20)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def render_meta(session: dict, workspace: Optional[str], plan_doc: Optional[str],
                task_url: Optional[str], revision: Optional[int] = None) -> str:
    """Render the Meta section: the jump-off pointers.

    The 4000-char ceiling means detail cannot all live in the description; Meta is the
    compensation for "the CR is the only review entry point" — it carries the absolute
    paths a reviewer (or a future agent) can follow.

    `revision` (v1.49.68) is stamped on re-pushes. Two jobs: it tells a reviewer which
    revision's test evidence the Testing section below describes (the section is
    re-rendered from the DB each revision, so it changes), and it gives `[R06]` a
    deterministic string to grep for when verifying the pushed description really did
    refresh — `cr --update-review` does NOT refresh it on its own (anti-pattern
    `cr-update-review-does-not-sync-description`), so that check is load-bearing.
    """
    lines = []
    if revision and revision > 1:
        lines.append(f"Revision: {revision} (description re-rendered for this revision)")
    if task_url:
        lines.append(f"Task: {task_url}")
    if plan_doc:
        lines.append(f"Plan doc: {plan_doc}")
    if workspace:
        lines.append(f"Workspace: {workspace}")
    delivery = session.get("delivery_mode")
    if delivery:
        lines.append(f"Delivery: {delivery}")
    reviewer = (session.get("config") or {}).get("reviewer")
    if reviewer:
        lines.append(f"Reviewer: {reviewer}")
    head = _git(workspace, "rev-parse", "--short", "HEAD")
    if head:
        # Two states, each reporting its own truth.
        #
        # ⚠️ `HEAD~1..HEAD` is the diff of the commit AT HEAD — NOT of the change being
        # described. `[E02]` renders BEFORE `[E07]` commits (the user has to see the
        # description before deciding to commit, so that order cannot be reversed), so at
        # render time HEAD is still the PARENT commit. Reporting it gave the reviewer a
        # precise-looking but wrong number: CR-299406107 rev 1 printed
        # `540fcb2 (3 files changed, 53 insertions(+), 2 deletions(-))` when the actual
        # change was 2 files / +321 −3 — `540fcb2` was the parent and that diffstat was its
        # own. Meta is `never_drop` in the spec, so the wrong number always reaches a human.
        #
        # Dirty  → the change is still in the working tree. Report the PENDING FILE COUNT from
        #          `status --porcelain`, not `diff --shortstat HEAD`: shortstat covers tracked
        #          modifications only, and at [E02] a change that ADDS files has them still
        #          untracked, so shortstat would print an undercount (or nothing at all for an
        #          all-new-files change) — reintroducing the very precise-but-wrong number this
        #          fix removes. A file count is derivable truthfully for every entry status.
        # Clean  → the change IS the commit at HEAD (the `[R06]` path, which renders after
        #          `[R05]` amends): keep the historical output byte-for-byte.
        #
        # Not "always use `diff --shortstat HEAD`": on a clean tree that prints nothing, which
        # would silently drop the diffstat and be worse than today.
        pending = _git(workspace, "status", "--porcelain")
        if pending:
            n = len(pending.splitlines())
            lines.append(f"Commit: (not yet committed at render time; HEAD={head})"
                         f" ({n} file{'s' if n != 1 else ''} pending)")
        else:
            stat = _git(workspace, "diff", "--shortstat", "HEAD~1", "HEAD")
            lines.append(f"Commit: {head}" + (f" ({stat})" if stat else ""))
    if not lines:
        lines.append("(no session metadata available)")
    return "\n".join(lines)


# ------------------------------------------------------------------- assembly


SECTION_RE_TMPL = r"^##\s+{name}\s*$"


def parse_document(text: str, spec: dict) -> dict:
    """Split an assembled description into {section_id: body} using the spec headings."""
    secs = ordered_sections(spec)
    heads = {s["id"]: s["heading"].lstrip("# ").strip() for s in secs}
    positions = []
    for sid, name in heads.items():
        m = re.search(SECTION_RE_TMPL.format(name=re.escape(name)), text, re.MULTILINE)
        if m:
            positions.append((m.start(), m.end(), sid))
    positions.sort()
    out = {}
    for i, (_start, end, sid) in enumerate(positions):
        stop = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        out[sid] = text[end:stop].strip()
    return out


def assemble(bodies: dict, spec: dict) -> str:
    parts = []
    for s in ordered_sections(spec):
        body = (bodies.get(s["id"]) or "").strip()
        if not body and not s["required"]:
            continue
        parts.append(f"{s['heading']}\n\n{body}")
    return "\n\n".join(parts) + "\n"


def apply_degradation(bodies: dict, spec: dict, evidence: dict) -> tuple:
    """Shrink the document to total_limit following the spec's fixed degrade order.

    Fixed order matters: the previous behaviour was an agent deciding ad hoc what to cut,
    which is why the same class of information survived in one CR and vanished in the
    next. Strength caveats and the verdict are never dropped.
    """
    applied = []
    doc = assemble(bodies, spec)
    limit = spec["total_limit"]
    if len(doc) <= limit:
        return doc, applied
    for step in spec.get("degrade_order") or []:
        action = step.get("action")
        target = step.get("target")
        if action == "collapse_executions_table" and target == "testing":
            bodies["testing"] = render_testing(evidence, spec, collapse=True)
            applied.append("collapse_executions_table(testing)")
        elif action == "truncate" and target in bodies:
            over = len(assemble(bodies, spec)) - limit
            if over > 0:
                body = bodies[target]
                keep = max(0, len(body) - over - len(" [truncated]"))
                bodies[target] = body[:keep].rstrip() + " [truncated]"
                applied.append(f"truncate({target})")
        doc = assemble(bodies, spec)
        if len(doc) <= limit:
            break
    return doc, applied


# ------------------------------------------------------------------- commands


def _session_context(uuid: str) -> dict:
    """Read session row + config from the state DB. Never raises: an unreadable DB must
    degrade to a rendered notice, not block a ship."""
    try:
        import state_machine as sm
    except Exception:
        return {}
    try:
        conn = sm.get_conn()
        try:
            # NOTE: there is no `workspace` column — it lives in metadata_json.workspace
            # (same place _workspace_of reads it from). Selecting a non-existent column
            # would raise and, because this function must never block a ship, would
            # silently degrade the whole Meta section to "(no session metadata)".
            row = conn.execute(
                "SELECT delivery_mode, task_id, plan_doc_path, metadata_json "
                "FROM shipcheck_active WHERE session_id = ?", (uuid,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return {}
        d = dict(row)
        try:
            meta = json.loads(d.get("metadata_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        d["config"] = meta.get("config") or {}
        d["workspace"] = meta.get("workspace")
        return d
    except Exception:
        return {}


def _evidence(uuid: str) -> dict:
    try:
        import state_machine as sm
        return sm.read_test_evidence(uuid)
    except Exception:
        return {"meta": None, "executions": []}


def cmd_render(args) -> int:
    try:
        spec = load_spec(Path(args.spec) if args.spec else SPEC_PATH)
    except ValueError as exc:
        print(f"❌ spec unusable: {exc}", file=sys.stderr)
        return RC_UNUSABLE

    session = _session_context(args.uuid)
    evidence = _evidence(args.uuid)
    workspace = args.workspace or session.get("workspace")
    plan_doc = args.plan_doc or session.get("plan_doc_path")
    task_url = args.task_url
    if not task_url and session.get("task_id"):
        task_url = f"https://taskei.amazon.dev/tasks/{session['task_id']}"

    bodies = {}
    for pair in args.section_file or []:
        if "=" not in pair:
            print(f"❌ --section-file expects id=path, got {pair!r}", file=sys.stderr)
            return RC_UNUSABLE
        sid, path = pair.split("=", 1)
        p = Path(path)
        if not p.exists():
            print(f"❌ --section-file {sid}: no such file {path}", file=sys.stderr)
            return RC_UNUSABLE
        bodies[sid] = p.read_text().strip()

    bodies["testing"] = render_testing(evidence, spec)
    bodies["meta"] = render_meta(session, workspace, plan_doc, task_url,
                                 revision=getattr(args, "revision", None))

    for s in ordered_sections(spec):
        if s["kind"] == "agent-authored" and s["id"] not in bodies:
            # A visible marker the `no_placeholder` check will reject, so an unfilled
            # section fails lint loudly instead of shipping an empty heading.
            bodies[s["id"]] = f"TODO: {s.get('guidance', 'fill in this section')}"

    doc, applied = apply_degradation(bodies, spec, evidence)

    if args.out:
        Path(args.out).write_text(doc)
        print(f"📝 rendered -> {args.out} ({len(doc)} chars, limit {spec['total_limit']})")
    else:
        sys.stdout.write(doc)
    if applied:
        print(f"⚠️ degradation applied: {', '.join(applied)}", file=sys.stderr)
    if not evidence.get("executions") and not evidence.get("meta"):
        print("⚠️ no structured test record for this session — Testing section degraded. "
              "Did [D05] run `record-test`?", file=sys.stderr)
    return RC_OK


def cmd_lint(args) -> int:
    try:
        spec = load_spec(Path(args.spec) if args.spec else SPEC_PATH)
    except ValueError as exc:
        print(f"❌ spec unusable: {exc}", file=sys.stderr)
        return RC_UNUSABLE

    p = Path(args.file)
    if not p.exists():
        print(f"❌ no such file: {args.file}", file=sys.stderr)
        return RC_UNUSABLE
    text = p.read_text()
    bodies = parse_document(text, spec)

    failures = []
    rows = []
    for s in ordered_sections(spec):
        sid = s["id"]
        body = bodies.get(sid)
        if body is None:
            if s["required"]:
                failures.append((sid, f"required section missing (expected heading {s['heading']!r})"))
                rows.append((sid, "-", "MISSING"))
            continue
        errs = []
        for cname in s["checks"]:
            checker = CHECKERS.get(cname)
            if checker is None:
                failures.append((sid, f"spec references unimplemented check {cname!r}"))
                continue
            err = checker(body, s, spec)
            if err:
                errs.append(err)
        for e in errs:
            failures.append((sid, e))
        rows.append((sid, f"{len(body)}/{s['char_quota']}", "FAIL" if errs else "ok"))

    total = len(text)
    if total > spec["total_limit"]:
        failures.append(("<document>", f"{total} chars exceeds total_limit {spec['total_limit']}"))

    print(f"🔎 lint {p.name} — {total}/{spec['total_limit']} chars")
    for sid, size, verdict in rows:
        print(f"   {'✅' if verdict == 'ok' else '❌'} {sid:<8} {size:>10}  {verdict}")
    if failures:
        print("")
        for sid, err in failures:
            print(f"❌ [{sid}] {err}", file=sys.stderr)
        print(f"⛔ lint FAILED ({len(failures)} issue(s)) — [E07] must not push",
              file=sys.stderr)
        return RC_CHECK_FAILED
    print("✅ lint passed")
    return RC_OK


def main() -> int:
    ap = argparse.ArgumentParser(description="Ship-check CR description render + lint")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("render", help="Render a CR description (machine sections from the state DB)")
    r.add_argument("--uuid", required=True)
    r.add_argument("--revision", type=int, default=None, metavar="N",
                   help="Revision number this render is for (>=2 on a re-push). Stamps Meta so "
                        "reviewers can tell which revision's evidence they are reading, and so "
                        "[R06] can verify the pushed description actually refreshed.")
    r.add_argument("--workspace", default=None)
    r.add_argument("--plan-doc", default=None)
    r.add_argument("--task-url", default=None)
    r.add_argument("--section-file", action="append", metavar="id=path",
                   help="Supply an agent-authored section body from a file, e.g. what=/tmp/what.md")
    r.add_argument("--out", default=None, help="Write to a file instead of stdout")
    r.add_argument("--spec", default=None, help="Override spec path (tests)")

    l = sub.add_parser("lint", help="Check an assembled description against the spec (exit 0/1/2)")
    l.add_argument("--uuid", required=False, default=None)
    l.add_argument("--file", required=True)
    l.add_argument("--spec", default=None, help="Override spec path (tests)")

    args = ap.parse_args()
    if args.cmd == "render":
        return cmd_render(args)
    if args.cmd == "lint":
        return cmd_lint(args)
    return RC_UNUSABLE


if __name__ == "__main__":
    sys.exit(main())

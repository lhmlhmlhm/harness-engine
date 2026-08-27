#!/usr/bin/env python3
"""
run-assertions.py — Execute [D10] Blast Radius Assertions A1-A13 (static-decidable subset).

Design (v1.42.1+):
- Ship-check plan `verification-layer-assertion-gate-executors` (2026-07-03) codifies the
  spec pseudocode in references/blast-radius-assertions.md into an actual executor.
- Agent used to read the pseudocode and "play interpreter" filling ✅/❌ manually.
  This script makes that deterministic.

Scope split (per plan "断言分类总表"):
  🟢 auto           A1, A2, A3, A4, A7, A8, A12(a)          — direct verdict output
  🟢 auto-detect    A9                                       — flag + require agent invocation record
  🟡 semi           A10(a)                                   — grep ground truth vs claim in --change-set
  🔴 manual         A6, A10(b), A12(b)(c), A13(a)(b)          — emit ⚠️ manual, agent decides
  ⏭️ delegated      A5                                       — belongs to [D00]/[D05]
  🟢 ast-audit      A11                                      — ast.parse transitive imports vs allowlist + guarded

Output JSON schema (frozen at commit-1; classify-gate.py consumes it):
  {
    "schema_version": "1.0",
    "workspace": "<abs path>",
    "generated_at": "<ISO 8601 UTC>",
    "change_set": {...},                 # echoed input (or {} if not provided)
    "assertions": [
      {
        "assertion_id": "A1" | ... | "A12(a)" | "A13(a)" | "A13(b)",
        "triggered": true | false,
        "verdict": "pass" | "fail" | "na" | "manual" | "deferred" | "skipped",
        "detail": "<one-line summary>",
        "evidence": {...}                # assertion-specific structured payload
      },
      ...
    ],
    "summary": {
      "auto_pass": N, "auto_fail": N, "manual": N, "na": N, "deferred": N, "skipped": N,
      "any_fail": bool
    }
  }

Usage:
  run-assertions.py --workspace <dir> [--change-set <file.json>] [--format md|json]
                    [--diff-since <rev>] [--output <file>]

Exit codes:
  0  = all triggered auto assertions pass (may still have manual items to review)
  1  = at least one triggered auto assertion FAIL
  2  = invocation error (bad args, workspace missing, etc.)
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import subprocess
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml is present on every supported runtime
    yaml = None  # A8 degrades to `na`; it never fails open as a false PASS.
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class AssertionResult:
    assertion_id: str
    triggered: bool
    verdict: str  # pass | fail | na | manual | deferred | skipped
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def run_git(workspace: Path, *args: str) -> tuple[int, str, str]:
    """Run git command. Returns (exit_code, stdout, stderr). Never raises."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode, result.stdout, result.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 127, "", f"git invocation failed: {e}"


def is_git_repo(workspace: Path) -> bool:
    code, _, _ = run_git(workspace, "rev-parse", "--is-inside-work-tree")
    return code == 0


_REPO_ROOT_CACHE: dict[str, Path] = {}


def git_repo_root(workspace: Path) -> Path:
    """The git repo root that `git diff` paths are relative to (v1.49.66).

    ⚠️ THE BUG THIS EXISTS TO KILL — `git diff --name-only` always emits **repo-root**
    relative paths, but every consumer here used to read them as `workspace / rel`. When
    the workspace is a SUBDIRECTORY of the repo (e.g. workspace `~/.kiro/skills/ship-check`
    inside repo `~/.kiro/skills`), that composes `ship-check/ship-check/...`, the read
    raises OSError, and the enclosing `except OSError: return False` conservatively scores
    the reference as REAL. Result: A4/A8/A9/A11/A12 false positives, measured at 10 vs 2 on
    the very same tree depending only on which directory you pointed at.

    A false *negative* would at least be visibly wrong; this failure mode invents a
    confident WRONG answer where "cannot verify" was the truth — the same pathology as
    a stale transcript proving "the user never replied".

    Falls back to `workspace` when git cannot answer (not a repo / git missing), which is
    exactly the old behaviour, so a non-git workspace is unaffected.
    """
    key = str(workspace)
    cached = _REPO_ROOT_CACHE.get(key)
    if cached is not None:
        return cached
    code, stdout, _ = run_git(workspace, "rev-parse", "--show-toplevel")
    root = Path(stdout.strip()) if code == 0 and stdout.strip() else workspace
    _REPO_ROOT_CACHE[key] = root
    return root


def git_diff_names(
    workspace: Path,
    since: Optional[str] = None,
    diff_filter: Optional[str] = None,
) -> list[str]:
    """Return git diff --name-only against HEAD (or vs `since`)."""
    args = ["diff", "--name-only"]
    if diff_filter:
        args.append(f"--diff-filter={diff_filter}")
    if since:
        args.append(since)
    else:
        args.append("HEAD")
    code, stdout, _ = run_git(workspace, *args)
    if code != 0:
        # Fallback: also include untracked/uncommitted via status
        code2, stdout2, _ = run_git(workspace, "status", "--porcelain=v1", "-uall")
        if code2 != 0:
            return []
        names: list[str] = []
        for line in stdout2.splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip()
            if diff_filter and "A" in diff_filter and not line.startswith("??"):
                continue
            names.append(path)
        return names
    return [line.strip() for line in stdout.splitlines() if line.strip()]


def git_diff_content(workspace: Path, since: Optional[str] = None) -> str:
    args = ["diff"]
    if since:
        args.append(since)
    args.append("HEAD")
    code, stdout, _ = run_git(workspace, *args)
    return stdout if code == 0 else ""


# ---------------------------------------------------------------------------
# Change-set loading
# ---------------------------------------------------------------------------


def load_change_set(path: Optional[Path]) -> dict[str, Any]:
    """
    change-set JSON shape (all fields optional):
      {
        "files": [".../path.py", ...],           # planned file list
        "old_values": [                          # values that must NOT survive (A1)
          {"value": "...", "scope": "repo|paths", "paths": ["glob", ...]}
        ],
        "auto_generated_files": ["dist/*", ...], # A2 offset — flagged as W1 not B4
                                                 # A4 offset — path tokens sourced ONLY from
                                                 # these files downgrade to `manual` (build
                                                 # artifacts may root paths at install-time
                                                 # dirs, not the repo root)
        "extra_path_roots": ["skills"],          # A4 — extra roots to resolve path tokens
                                                 # against, besides workspace/ and absolute.
                                                 # Explicit only (no directory auto-scan).
        "numeric_claims": [                      # A10(a) semi-auto
          {"claim": 7, "unit": "红测样本", "search_space": "test/fixtures/*.json",
           "anchor_pattern": "regex"}
        ],
        "api_constants": [                       # A7 consumer layers
          {"new_value": "foo", "consumer_globs": ["src/**/*.ts"]}
        ],
        "modules_under_test": [".../src/x.py"],  # A11 targets (if plan can name them)
      }

    Authoritative, agent-facing schema (each key + the assertion that consumes it + what
    omitting it costs): references/[verification]change-set-schema.md. Keep the two in sync —
    tests/test_run_assertions.py::test_change_set_schema_keys_are_all_consumed fails if that
    reference names a key this module never reads.

    NOTE: there is deliberately no "test_files" key. It was documented here as an "A11 override"
    for a long time but was never read — A11 derives its test-file list from `git diff` plus
    untracked files only (see assertion_a11). Use `modules_under_test` to scope A11.
    """
    if path is None or not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Individual assertion implementations
# ---------------------------------------------------------------------------


def assertion_a1(workspace: Path, change_set: dict[str, Any]) -> AssertionResult:
    """A1 — old values have zero residual matches (exact match, not split words)."""
    old_values = change_set.get("old_values") or []
    if not old_values:
        return AssertionResult(
            assertion_id="A1",
            triggered=False,
            verdict="na",
            detail="no old_values declared in change-set",
        )
    misses: list[dict[str, Any]] = []
    scanned: list[str] = []
    for spec in old_values:
        value = spec.get("value")
        if not value:
            continue
        scope = spec.get("scope", "repo")
        paths = spec.get("paths") or []
        # Guard: short values must have path filter
        if len(value) < 6 and scope == "repo" and not paths:
            misses.append({
                "value": value,
                "issue": "value < 6 chars and no path filter — refusing exact grep to avoid noise",
            })
            continue
        # Build the residual-scan command.
        #
        # v1.49.73: prefer `git grep --no-index --exclude-standard` over a raw `grep -r`.
        # Raw `grep -r` walks EVERYTHING, including gitignored build artefacts, and A1's only
        # failure channel is a 60s timeout that is reported as `2 old value(s) still present`
        # — i.e. "the search never finished" is indistinguishable, in the verdict table, from
        # "the old value survived". Measured 2026-08-20 in `~/.kiro/skills`: one session's
        # `ship-check/state/worktrees/<uuid>/` held a full brazil checkout (1.5 GB / 116,435
        # files), so every A1 in this repo timed out and classified as a B2 blocker with no
        # defect behind it. `--no-index` keeps A1's semantics (search the WORKING TREE,
        # untracked files included — a stale value in a not-yet-added file must still be
        # caught) while `--exclude-standard` drops exactly the regenerable artefacts
        # .gitignore already declares worthless. `-e` is required: `--` means pathspec to
        # git grep, not end-of-options.
        use_git_grep = is_git_repo(workspace)
        if use_git_grep:
            scan_args: list[str] = ["git", "-C", str(workspace), "grep",
                                    "--no-index", "--exclude-standard", "-F", "-l", "-e", value]
            if paths:
                scan_args += ["--", *paths]
        else:
            scan_args = ["grep", "-r", "-F", "-l", "--", value]
            if paths:
                scan_args += [str(workspace / p) if not os.path.isabs(p) else p for p in paths]
            else:
                scan_args.append(str(workspace))
        try:
            proc = subprocess.run(scan_args, capture_output=True, text=True, timeout=60)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            misses.append({"value": value, "issue": f"grep exec failed: {e}"})
            continue
        # grep -l: exit 0 = matches, exit 1 = no matches, exit ≥2 = error
        scanned.append(value)
        if proc.returncode == 0:
            hit_files = [p for p in proc.stdout.splitlines() if p.strip()]
            misses.append({"value": value, "hit_files": hit_files})
        elif proc.returncode == 1:
            continue  # good: no matches
        else:
            misses.append({"value": value, "issue": f"grep exit {proc.returncode}: {proc.stderr.strip()[:200]}"})
    if not misses:
        return AssertionResult(
            assertion_id="A1",
            triggered=True,
            verdict="pass",
            detail=f"grep {len(scanned)} old value(s) → 0 matches",
            evidence={"scanned_values": scanned},
        )
    return AssertionResult(
        assertion_id="A1",
        triggered=True,
        verdict="fail",
        detail=f"{len(misses)} old value(s) still present",
        evidence={"misses": misses},
    )


def assertion_a2(workspace: Path, change_set: dict[str, Any]) -> AssertionResult:
    """A2 — diff --stat file list vs plan change-set (mismatch beyond auto-gen → fail; auto-gen → W1)."""
    planned = set(change_set.get("files") or [])
    auto_gen_globs = change_set.get("auto_generated_files") or []
    if not planned:
        return AssertionResult(
            assertion_id="A2",
            triggered=False,
            verdict="na",
            detail="no `files` in change-set — cannot compare diff scope",
        )
    if not is_git_repo(workspace):
        return AssertionResult(
            assertion_id="A2",
            triggered=True,
            verdict="deferred",
            detail="workspace is not a git repo — cannot compute diff scope",
        )
    actual_list = git_diff_names(workspace)
    # Also include untracked (git status --porcelain) — new files often live in workspace before add
    code, status, _ = run_git(workspace, "status", "--porcelain=v1", "-uall")
    if code == 0:
        for line in status.splitlines():
            if line.startswith("??") and len(line) >= 4:
                actual_list.append(line[3:].strip())
    actual = set(actual_list)
    extras = actual - planned
    missing = planned - actual

    def is_auto_gen(path: str) -> bool:
        return any(fnmatch.fnmatch(path, g) for g in auto_gen_globs)

    unexpected_source = [p for p in extras if not is_auto_gen(p)]
    unexpected_autogen = [p for p in extras if is_auto_gen(p)]
    if not unexpected_source and not missing:
        detail = "diff scope == plan"
        if unexpected_autogen:
            detail += f" (+{len(unexpected_autogen)} auto-gen → W1 flag)"
        return AssertionResult(
            assertion_id="A2",
            triggered=True,
            verdict="pass",
            detail=detail,
            evidence={
                "planned": sorted(planned),
                "actual": sorted(actual),
                "auto_gen_extras": sorted(unexpected_autogen),
            },
        )
    return AssertionResult(
        assertion_id="A2",
        triggered=True,
        verdict="fail",
        detail=f"scope drift: {len(unexpected_source)} unexpected source file(s), {len(missing)} missing",
        evidence={
            "planned": sorted(planned),
            "actual": sorted(actual),
            "unexpected_source": sorted(unexpected_source),
            "unexpected_autogen": sorted(unexpected_autogen),
            "missing": sorted(missing),
        },
    )


UNCOMMENT_RE = re.compile(
    r"^-\s*(?:#\s*|//\s*|/\*\s*|<!--\s*)?(TODO|FIXME|DISABLED|XXX)[:\s]|"
    r"^-\s*#\s*\S|"
    r"^-\s*//\s*\S|"
    r"^-\s*/\*\s*\S|"
    r"^-\s*<!--\s*\S"
)


def assertion_a3(workspace: Path, change_set: dict[str, Any]) -> AssertionResult:
    """A3 — no `+` line accidentally removed a comment/disabled marker."""
    if not is_git_repo(workspace):
        return AssertionResult(
            assertion_id="A3",
            triggered=False,
            verdict="deferred",
            detail="workspace is not a git repo",
        )
    diff = git_diff_content(workspace)
    if not diff.strip():
        return AssertionResult(
            assertion_id="A3",
            triggered=False,
            verdict="na",
            detail="no diff content to scan",
        )
    # Pair each removed line with same-file added lines within a small window.
    # A simple heuristic: for every `- # xxx` we look for `+ xxx` (same content without comment prefix)
    # in the same hunk. This catches the "uncomment by removing #" pattern.
    hits: list[dict[str, Any]] = []
    current_file: Optional[str] = None
    removed_comments: list[tuple[int, str]] = []
    added_lines: list[tuple[int, str]] = []
    for idx, raw in enumerate(diff.splitlines()):
        if raw.startswith("+++ b/"):
            # New file section — flush prior file's analysis
            if removed_comments and added_lines:
                for _, rc in removed_comments:
                    stripped = re.sub(r"^\s*(?:#\s?|//\s?|/\*\s?|<!--\s?)", "", rc[1:]).strip()
                    if not stripped:
                        continue
                    for _, al in added_lines:
                        if stripped and stripped == al[1:].strip():
                            hits.append({"file": current_file, "removed": rc, "added": al})
            current_file = raw[6:].strip()
            removed_comments = []
            added_lines = []
            continue
        if raw.startswith("-") and not raw.startswith("---"):
            # candidate: "- # something" or "- // something" or "- disabled"
            body = raw[1:]
            if re.match(r"\s*(?:#|//|/\*|<!--|\bdisabled\b|\bTODO\b|\bFIXME\b|\bXXX\b)", body):
                removed_comments.append((idx, raw))
        elif raw.startswith("+") and not raw.startswith("+++"):
            added_lines.append((idx, raw))
    # Flush last file
    if removed_comments and added_lines:
        for _, rc in removed_comments:
            stripped = re.sub(r"^\s*(?:#\s?|//\s?|/\*\s?|<!--\s?)", "", rc[1:]).strip()
            if not stripped:
                continue
            for _, al in added_lines:
                if stripped and stripped == al[1:].strip():
                    hits.append({"file": current_file, "removed": rc, "added": al})
    if not hits:
        return AssertionResult(
            assertion_id="A3",
            triggered=True,
            verdict="pass",
            detail="no accidental uncomment pattern detected",
        )
    return AssertionResult(
        assertion_id="A3",
        triggered=True,
        verdict="fail",
        detail=f"{len(hits)} suspicious uncomment(s) — verify intent",
        evidence={"hits": hits[:20]},
    )


NEW_PATH_RE = re.compile(
    r"^\+.*?(?:"
    r"(?:from|import)\s+[\"']?([\w./\-]+\.[a-zA-Z]+)|"  # ES/py imports with ext
    r"(?:require\(|open\(|read_text\(|open_file\()\s*[\"']([\w./\-]+\.[a-zA-Z]+)|"
    r"(?:^|[^\w])[\"']([\w./\-]+\.[a-zA-Z]{1,4})[\"']"
    r")"
)


def is_auto_generated_only(source_files: set[str], auto_gen_globs: list[str]) -> bool:
    """True when every diff file a token came from matches an auto_generated_files glob.

    AND semantics on purpose: if a token also appears in a hand-written file, A4 must
    still fail on it — only tokens exclusively sourced from build artifacts are deferred.
    Empty inputs (no globs declared, or unattributed token) → False (stay strict).
    """
    if not auto_gen_globs or not source_files:
        return False
    return all(
        any(fnmatch.fnmatch(f, g) for g in auto_gen_globs)
        for f in source_files
        if f
    ) and any(f for f in source_files)


def _is_importable_module(token: str, workspace: Path = None,
                         extra_roots: list = None) -> bool:
    """v1.49.56: True when `token` is a module reference, not a filesystem path.

    A4 greps added diff lines for path-ish tokens, so dotted module names picked up from
    import statements (`html.parser`, `scripts.classify_op`) got reported as nonexistent
    paths. Its dotted→filesystem conversion cannot resolve them (stdlib lives outside the
    workspace; a package-relative import needs the package root on sys.path).

    Two probes, both cheap and import-free:
      1. `importlib.util.find_spec(root)` — catches stdlib / installed packages
         (`html.parser`).
      2. root resolves to a DIRECTORY in the workspace or a declared extra root — catches
         in-repo package references (`scripts.classify_op` where `.../scripts/` is a dir).
    """
    if "/" in token or not token or token[0].isdigit() or "." not in token:
        return False
    root = token.split(".")[0]
    if not root.isidentifier():
        return False
    try:
        import importlib.util
        # find_spec on the ROOT package is enough, and imports nothing.
        if importlib.util.find_spec(root) is not None:
            return True
    except Exception:  # noqa: BLE001 — never let the probe break the assertion
        pass
    if workspace is not None:
        for base in [workspace] + [workspace / r for r in (extra_roots or [])]:
            try:
                if (base / root).is_dir():
                    return True
            except OSError:
                continue
    return False


#: A JS/TS test file. Both spellings are load-bearing: jest/vitest recognise
#: `*.test.ts` / `*.spec.tsx` by suffix, while many packages instead group specs under a
#: `__tests__/` directory with no suffix marker at all (the shape that produced the
#: false positive this exists for).
_JS_TEST_PATH_RE = re.compile(r"(?:\.(?:test|spec)\.[cm]?[jt]sx?$)|(?:(?:^|/)__tests__/)")

#: Signals that a JS/TS spec builds its fixtures IN MEMORY rather than reading them off
#: disk. This is the analogue of `has_tempfile` in shape (1), and it is what keeps the
#: deferral narrow: a spec that only ever reads real files matches none of these, so a
#: broken path in it still fails.
_JS_FIXTURE_BUILDER_RE = re.compile(
    r"\bjest\.(?:mock|fn|spyOn)\s*\(|\bvi\.(?:mock|fn|spyOn)\s*\(|"
    r"\bBuffer\.(?:alloc|from|concat)\s*\(|\bnew\s+(?:Uint8Array|ArrayBuffer)\s*\(|"
    r"\bmockResolvedValue|\bmockReturnValue|\bmockImplementation"
)

#: A filesystem read on this line. This — not the line's position in the file — is what
#: actually distinguishes "a path the code reads" from "a name in fixture data".
#:
#: Position alone was the first cut and it misjudged MODULE-LEVEL TEST DATA: a spec's shared
#: `const validBase = { ..., s3Key: 'skills/a.zip' }` sits above the first `describe(`, so a
#: position test rejects it even though it is the same in-memory fixture as an identical
#: literal ten lines lower inside an `it(`. Measured on plan
#: `skillhub-package-file-tree-from-zip-directory`: it left exactly one residue token, which
#: is worse than either outcome — the gate still blocks, but now on a single case whose
#: classification is indistinguishable from the four it just deferred.
_JS_FS_READ_RE = re.compile(
    r"\b(?:readFile|readFileSync|readdir|readdirSync|createReadStream|existsSync|statSync|"
    r"lstatSync|realpathSync|loadFile|readJson|readJsonSync)\s*\(|"
    r"\brequire\s*\(|\bimport\s*\(|\bfrom\s+['\"]|^\s*import\b"
)


def _js_test_fixture_only(token: str, lines: list[str]) -> bool:
    """True when every occurrence of `token` in a JS/TS spec is an in-memory fixture name.

    Deliberately requires all of the following together, so this cannot become a rubber
    stamp for test files:

    1. the file builds fixtures in memory (`_JS_FIXTURE_BUILDER_RE`) — a spec that reads
       real files off disk matches nothing here and is left to fail;
    2. the occurrence performs no filesystem read (`_JS_FS_READ_RE`) — an `import`, a
       `require(`, a `readFileSync(` is a real reference wherever it appears. Position in the
       file is NOT the test: a spec's shared `const validBase = { s3Key: 'skills/a.zip' }`
       above the first `describe(` is the same in-memory fixture as an identical literal
       inside an `it(`, and judging it by position left exactly one residue token whose
       classification was indistinguishable from the ones it deferred;
    3. the occurrence is inside a quoted string literal — a bare identifier is real code.

    A `//` comment is accepted on its own, mirroring shape (2)'s treatment of `#`.

    ⚠️ (1) and (2) are the load-bearing gates; **(3) is defence-in-depth and is rarely
    decisive here.** A4's own extractor (`NEW_PATH_RE`) only ever yields an UNQUOTED token
    from an `import` / `from` statement — every other alternative already requires quotes —
    and (2) rejects those outright. So (3) can almost never be the deciding condition
    through A4, and it is kept for callers that reach this helper with a token found some
    other way. It is unit-tested directly for that reason; do not read it as a gate A4
    exercises.

    Latitude note: (1)+(2) still admit a genuine path passed to something this regex does not
    know as a read. That is the same latitude shape (1) already grants a Python test using
    `tmp_path` — symmetric with existing behaviour rather than looser than it.
    """
    has_fixture_builder = any(_JS_FIXTURE_BUILDER_RE.search(ln) for ln in lines)

    for line in lines:
        if token not in line:
            continue
        # (a) prose — the token is confined to a `//` line comment. Checked first and
        # independently of the fixture signals, mirroring shape (2).
        before_comment = line.split("//", 1)[0]
        if token not in before_comment:
            continue
        if not has_fixture_builder:
            return False
        # (b) a filesystem read is a real reference no matter where it sits.
        if _JS_FS_READ_RE.search(before_comment):
            return False
        # (c) in-memory fixture name — must be inside a string literal. Template literals
        # included: a path assembled with backticks is still a name, not a disk read.
        in_literal = any(
            token in lit
            for lit in re.findall(r"'[^']*'|\"[^\"]*\"|`[^`]*`", before_comment)
        )
        if not in_literal:
            return False
    return True


def _only_in_self_check_or_prose(token: str, source_files: set, workspace: Path) -> bool:
    """v1.49.56: True when `token` only appears where it is NOT meant to exist on disk.

    Three shapes, all observed as A4 false positives:

    1. **self-check fixtures** — a script carrying `--self-check` builds fixtures inside
       `tempfile.TemporaryDirectory()`, so names like `op-a.json` / `wrong_name.html` are
       created at runtime and MUST NOT exist in the repo. Detected as: the file imports
       `tempfile` AND every occurrence sits at/after the `_self_check` / `def test_` line.
       (session f35ae5e0: 31 of 35 residue tokens were these, zero were real defects — yet
       they produced a B3 `[D14]` block.)
    2. **prose** — the token only occurs inside a `#` comment or a quoted string (an
       explanatory comment, or text inside a violation/error message).
    3. **JS/TS test fixtures** (v1.49.71) — the SAME shape as (1) in a jest/vitest suite: a
       spec file builds in-memory fixtures and the token is a name inside one of them (a
       filename inside a hand-built zip, a fake S3 key, a mocked response path). Shape (1)
       could not see these because every one of its signals is Python-only — `def test_` /
       `def _self_check` regions, `tempfile` / `tmp_path` imports, `#` comments — while a
       TypeScript suite spells them `it(` / `describe(`, `jest.mock(` / `Buffer.alloc`, and
       `//`. So a token that Python-side would defer produced a B3 block purely because the
       fixture was written in TypeScript.

    Returns False if ANY occurrence is real code outside those contexts, so a genuinely
    broken reference still fails. Unreadable files → False (stay conservative).
    """
    if not source_files:
        return False
    # v1.49.66: `source_files` are git-diff names → repo-root relative, NOT workspace
    # relative. Reading them as `workspace / rel` raised OSError whenever the workspace was
    # a repo subdirectory, and the `except OSError: return False` below then scored EVERY
    # token as a real reference — a fabricated certainty. See git_repo_root().
    repo_root = git_repo_root(workspace)
    for rel in source_files:
        if not rel:
            return False  # unknown provenance → cannot vouch for it
        try:
            lines = (repo_root / rel).read_text(errors="replace").splitlines()
        except (OSError, UnicodeError):
            return False
        # v1.49.71: JS/TS spec files are handled by their own shape (3) below, because none
        # of the Python signals this branch tests for can ever appear in one.
        if _JS_TEST_PATH_RE.search(str(rel)):
            if _js_test_fixture_only(token, lines):
                continue
            return False
        region_start = None
        for i, line in enumerate(lines):
            if re.match(r"\s*def\s+(_?self_check|test_)", line):
                region_start = i
                break
        # Does this file build throwaway fixtures at all? Match any import form
        # (`import tempfile`, `import json, tempfile`, `from tempfile import ...`) or direct
        # `TemporaryDirectory` use — a narrow `^import tempfile$` regex missed the comma form.
        # v1.49.62: also match pytest's `tmp_path` / `tmpdir` / `tmp_path_factory` fixtures.
        # They need NO import, so a pytest test building a deliberately-absent fixture name
        # scored has_tempfile=False and its token was reported as a missing path (session
        # 0e757d6f: `does-not-exist.md` in test_build_gates.py, which has 0 `tempfile`
        # references). The fixture is the same idea as TemporaryDirectory — a path created,
        # or deliberately NOT created, at runtime under a temp root.
        has_tempfile = any(
            re.search(r"\btempfile\b|\bTemporaryDirectory\b|\bmkdtemp\b"
                      r"|\btmp_path_factory\b|\btmp_path\b|\btmpdir\b", ln) for ln in lines)
        for i, line in enumerate(lines):
            if token not in line:
                continue
            # (1) inside the self-check region of a tempfile-using script → runtime fixture
            if has_tempfile and region_start is not None and i >= region_start:
                continue
            # (2) prose — the token is confined to a `#` comment
            before_hash = line.split("#", 1)[0]
            if token not in before_hash:
                continue
            # (3) prose — the token sits inside a SENTENCE-like string literal (an error /
            # violation message), not a bare path argument. The distinction matters: a real
            # path reference in code is ALWAYS inside a string literal too
            # (`Path("config/x.yaml")`), so "appears in a string" alone would defer every
            # genuine broken reference. Require the enclosing literal to look like prose:
            # several whitespace-separated words around the token.
            prose_string = False
            for lit in re.findall(r"'[^']*'|\"[^\"]*\"", before_hash):
                if token in lit and len(lit.strip("'\"").split()) >= 4:
                    prose_string = True
                    break
            if prose_string:
                continue
            return False  # real code reference → keep it a failure
    return True


def assertion_a4(workspace: Path, change_set: dict[str, Any]) -> AssertionResult:
    """A4 — for every new path/file reference added, `ls` verifies it exists.

    Two change-set offsets keep A4 from false-positiving on build artifacts:
      * `auto_generated_files` — a token whose source files are ALL auto-generated
        is downgraded to `manual` instead of `fail`. Build artifacts (e.g. a skill
        bundle's FILES_MANIFEST) legitimately root their internal paths at an
        install-time directory, not the repo root.
      * `extra_path_roots` — extra roots to resolve tokens against (e.g. `skills`
        for a repo whose bundles mirror `skills/`). Explicit only: A4 never
        auto-scans workspace subdirectories, which would make it near-vacuous.

    v1.49.62: tokens are ALSO resolved against the directory of the file that mentions
    them (sibling-relative). That is not an auto-scan — it is the one root a cross-file
    reference is actually written relative to, and an absent sibling still fails.
    """
    if not is_git_repo(workspace):
        return AssertionResult(
            assertion_id="A4",
            triggered=False,
            verdict="deferred",
            detail="workspace is not a git repo",
        )
    diff = git_diff_content(workspace)
    if not diff.strip():
        return AssertionResult(
            assertion_id="A4",
            triggered=False,
            verdict="na",
            detail="no diff content",
        )
    auto_gen_globs = change_set.get("auto_generated_files") or []
    extra_roots = change_set.get("extra_path_roots") or []
    # token -> set of diff files the token was added in (for auto-gen attribution)
    candidates: dict[str, set[str]] = {}
    current_file = ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            # `+++ b/path/to/file` — track which file subsequent `+` lines belong to
            target = line[4:].strip()
            if target.startswith("b/"):
                target = target[2:]
            current_file = "" if target == "/dev/null" else target
            continue
        if not line.startswith("+"):
            continue
        for match in NEW_PATH_RE.finditer(line):
            for group in match.groups():
                if group:
                    candidates.setdefault(group, set()).add(current_file)
    # Whitelist obvious noise
    filtered: list[str] = []
    for c in candidates:
        # Skip common non-file tokens
        if re.match(r"^\d", c):
            continue
        if len(c) < 4:
            continue
        if c.startswith("http") or c.startswith("//"):
            continue
        filtered.append(c)
    if not filtered:
        return AssertionResult(
            assertion_id="A4",
            triggered=False,
            verdict="na",
            detail="no new path references detected in diff",
        )
    missing: list[str] = []
    auto_gen_missing: list[str] = []
    verified: list[str] = []
    fixture_deferred: list[str] = []
    for path_str in filtered:
        # Try workspace-relative first, then absolute, then declared extra roots
        candidates_to_try = [
            workspace / path_str,
        ]
        # v1.49.71: absolute ONLY. This used to be a bare `Path(path_str)`, which pathlib
        # resolves against the PROCESS CWD — so A4's answer depended on where it was invoked
        # from, and a token matching any file in that directory was silently scored `verified`.
        # Observed in this very function's own repo: run from `~/.kiro/skills/ship-check`, the
        # fixture name `SKILL.md` resolved against `ship-check/SKILL.md` and a path that exists
        # nowhere in the target workspace was reported as a verified reference. That is the
        # dangerous direction — a false `missing` gets investigated, a false `verified` is
        # silent — and it made the same change-set produce different verdicts between runs.
        if os.path.isabs(path_str):
            candidates_to_try.append(Path(path_str))
        # v1.49.66: ADDITIVE, not a replacement. `path_str` is a token MENTIONED inside
        # added diff content, so its intended root is ambiguous by nature: a doc under
        # `ship-check/` writing `data/step-coding.md` means workspace-relative, while one
        # writing `ship-check/data/step-coding.md` means repo-root-relative. Both spellings
        # occur, so swapping the base (as the git-diff-name sites below correctly do) would
        # merely trade one class of false positive for another. Appending is monotonic — it
        # can only resolve more real references, never fewer.
        repo_root = git_repo_root(workspace)
        if repo_root != workspace:
            candidates_to_try.append(repo_root / path_str)
        candidates_to_try += [workspace / root / path_str for root in extra_roots]
        # v1.49.62: resolve against the directory of the file that MENTIONS the token.
        # A4 already tracks that file (`candidates[token]`, used for auto-gen attribution)
        # but never used it for resolution — so a doc at `skills/x/README.md` referring to
        # its SIBLING `seed.memory-rules.md` could not resolve, and the only cure was to
        # hand-declare `extra_path_roots: ["skills/x"]` in every plan that touched it
        # (session 0e757d6f did exactly that). Cross-file references are overwhelmingly
        # relative to the referring file, so this is the natural root, not a whitelist —
        # and it weakens nothing: a sibling that genuinely does not exist still fails.
        for src in sorted(candidates[path_str]):
            if not src:
                continue
            # v1.49.66: `src` is a git-diff name → repo-root relative. Under the old
            # `workspace / src` the sibling root silently became a non-existent directory
            # whenever workspace was a repo subdir, so sibling resolution never fired.
            src_dir = (repo_root / src).parent
            candidates_to_try.append(src_dir / path_str)
        # For import-style dotted paths like `foo.bar.baz`, try converting to filesystem
        if "/" not in path_str and "." in path_str:
            parts = path_str.rsplit(".", 1)
            if len(parts) == 2 and len(parts[1]) <= 4:
                fs_form = parts[0].replace(".", "/") + "." + parts[1]
                candidates_to_try.append(workspace / fs_form)
                # ADDITIVE for the same reason as `path_str` above — derived from a content
                # token, so both roots are plausible.
                if repo_root != workspace:
                    candidates_to_try.append(repo_root / fs_form)
        if any(p.exists() for p in candidates_to_try):
            verified.append(path_str)
        elif _is_importable_module(path_str, workspace, extra_roots):
            # v1.49.56: dotted Python module name, not a filesystem path (`html.parser`
            # stdlib; `scripts.classify_op` from an import statement). The dotted→path
            # conversion above cannot resolve these, so they were reported as missing.
            verified.append(path_str)
        elif _only_in_self_check_or_prose(path_str, candidates[path_str], workspace):
            # v1.49.56: the token is a runtime fixture built inside a `--self-check`
            # (tempfile.TemporaryDirectory) or appears only in a comment / string literal.
            # Such a name is SUPPOSED not to exist on disk — see the docstring.
            fixture_deferred.append(path_str)
        elif is_auto_generated_only(candidates[path_str], auto_gen_globs):
            auto_gen_missing.append(path_str)
        else:
            missing.append(path_str)
    if missing:
        return AssertionResult(
            assertion_id="A4",
            triggered=True,
            verdict="fail",
            detail=f"{len(missing)} referenced path(s) do not exist",
            evidence={
                # v1.49.56: no longer truncated. The old `[:20]` cap made an exhaustive
                # classification impossible from the report itself — a 35-token residue had to
                # be re-derived by re-running this extraction by hand (session f35ae5e0). A
                # blocker's evidence must be complete enough to act on.
                "missing": missing,
                "verified_count": len(verified),
                "auto_generated_deferred": auto_gen_missing,
                "self_check_or_prose_deferred": fixture_deferred,
            },
        )
    if auto_gen_missing or fixture_deferred:
        # v1.49.71: the two deferrals are REPORTED SEPARATELY. They used to be concatenated
        # into `auto_gen_missing` under an `auto_generated_deferred` key, with a `detail`
        # asserting the tokens "come only from auto_generated_files (build-artifact path
        # roots)" — a cause that is simply false for a fixture deferral, and doubly so when
        # the change-set declares no `auto_generated_files` at all. Reading that verdict costs
        # an investigation cycle to discover the stated reason cannot apply (measured: plan
        # `skillhub-package-file-tree-from-zip-directory`, where all five tokens were JS/TS
        # fixture names and none were build artifacts). A manual verdict exists for an agent to
        # judge, so it must name the mechanism that actually fired.
        #
        # Not truncated, for the same reason the `missing` branch above stopped truncating: a
        # verdict the agent has to adjudicate needs complete evidence.
        reasons = []
        if auto_gen_missing:
            reasons.append(
                f"{len(auto_gen_missing)} from auto_generated_files (build-artifact path roots)")
        if fixture_deferred:
            reasons.append(
                f"{len(fixture_deferred)} runtime/test fixture name(s) (not meant to exist on disk)")
        return AssertionResult(
            assertion_id="A4",
            triggered=True,
            verdict="manual",
            detail=(
                f"{len(verified)} new path reference(s) verified; "
                f"{len(auto_gen_missing) + len(fixture_deferred)} unresolved token(s) deferred — "
                + "; ".join(reasons)
                + " — agent eyeballs, not a blocker"
            ),
            evidence={
                "auto_generated_deferred": auto_gen_missing,
                "self_check_or_prose_deferred": fixture_deferred,
                "verified_sample": verified[:10],
            },
        )
    return AssertionResult(
        assertion_id="A4",
        triggered=True,
        verdict="pass",
        detail=f"{len(verified)} new path reference(s) verified",
        evidence={"verified_sample": verified[:10]},
    )


def assertion_a5() -> AssertionResult:
    """A5 — delegated to [D00]/[D05] test framework execution. Never runs here."""
    return AssertionResult(
        assertion_id="A5",
        triggered=False,
        verdict="skipped",
        detail="A5 delegated to [D00]/[D05] test framework — not run by run-assertions",
    )


def assertion_a6(workspace: Path, change_set: dict[str, Any]) -> AssertionResult:
    """A6 — function signature change compat: MANUAL (agent judges type compat)."""
    if not is_git_repo(workspace):
        return AssertionResult(
            assertion_id="A6",
            triggered=False,
            verdict="deferred",
            detail="workspace is not a git repo",
        )
    diff = git_diff_content(workspace)
    triggered = bool(re.search(
        r"^[+-]\s*(?:def |function |interface |type |class |const .* = \(|export function )",
        diff,
        re.MULTILINE,
    ))
    if not triggered:
        return AssertionResult(
            assertion_id="A6",
            triggered=False,
            verdict="na",
            detail="no signature-level diff detected",
        )
    return AssertionResult(
        assertion_id="A6",
        triggered=True,
        verdict="manual",
        detail="⚠️ manual review: signature/type diff detected — agent must verify caller compatibility",
    )


def assertion_a7(workspace: Path, change_set: dict[str, Any]) -> AssertionResult:
    """A7 — API constant new value has consumer-layer matches > 0."""
    api_constants = change_set.get("api_constants") or []
    if not api_constants:
        return AssertionResult(
            assertion_id="A7",
            triggered=False,
            verdict="na",
            detail="no api_constants declared in change-set",
        )
    fails: list[dict[str, Any]] = []
    passes: list[dict[str, Any]] = []
    for spec in api_constants:
        new_value = spec.get("new_value")
        globs = spec.get("consumer_globs") or []
        if not new_value or not globs:
            fails.append({"spec": spec, "issue": "missing new_value or consumer_globs"})
            continue
        # Expand globs
        matched_files: list[Path] = []
        for pattern in globs:
            matched_files.extend(workspace.glob(pattern))
        if not matched_files:
            fails.append({"new_value": new_value, "globs": globs, "issue": "no consumer files matched glob"})
            continue
        hit_count = 0
        for fp in matched_files:
            try:
                if new_value in fp.read_text(encoding="utf-8", errors="ignore"):
                    hit_count += 1
            except (OSError, UnicodeDecodeError):
                continue
        if hit_count == 0:
            fails.append({"new_value": new_value, "scanned_files": len(matched_files), "hit_count": 0})
        else:
            passes.append({"new_value": new_value, "hit_files": hit_count})
    if fails:
        return AssertionResult(
            assertion_id="A7",
            triggered=True,
            verdict="fail",
            detail=f"{len(fails)} api constant(s) have no consumer matches",
            evidence={"fails": fails, "passes": passes},
        )
    return AssertionResult(
        assertion_id="A7",
        triggered=True,
        verdict="pass",
        detail=f"{len(passes)} api constant(s) verified in consumer layer",
        evidence={"passes": passes},
    )


def assertion_a8(workspace: Path, change_set: dict[str, Any]) -> AssertionResult:
    """A8 — every tool a skill manifest declares is documented in its status block.

    Set semantics (v1.49.67), NOT count equality: the doc-side tables are
    per-DOMAIN with several tools per cell, so a row count was never a tool
    count. See plan run-assertions-a8-count-to-nameset.
    """
    if not is_git_repo(workspace):
        return AssertionResult(
            assertion_id="A8",
            triggered=False,
            verdict="deferred",
            detail="workspace is not a git repo",
        )
    changed = git_diff_names(workspace)
    manifests = [
        p for p in changed
        if re.search(r"skills/.+/manifest\.md$", p) or p.endswith(".skill")
    ]
    if not manifests:
        return AssertionResult(
            assertion_id="A8",
            triggered=False,
            verdict="na",
            detail="no skill manifest / .skill changed",
        )
    if yaml is None:
        # Surface the missing dep explicitly rather than reporting a silent `na`
        # (an unavailable parser must not read as "assertion not applicable").
        return AssertionResult(
            assertion_id="A8",
            triggered=True,
            verdict="skipped",
            detail="A8 skipped: pyyaml unavailable, cannot parse manifest front matter",
            evidence={"manifests": manifests},
        )

    # A tool name as written in docs: `list_todo` / `awsentral_mcp__search_tasks`.
    tool_token_re = re.compile(r"`([a-z][a-z0-9_]*(?:__[a-z0-9_]+)?)`")
    # Exact heading match; the trailing group tolerates `## MCP Status (phase 4, v0.5.0)`.
    section_re = re.compile(
        r"^#{2,3}\s+(?:MCP Status|MCP Dependency|Tools)\s*(?:\(.*\))?\s*$",
        re.MULTILINE,
    )

    def manifest_tools(fp: Path) -> set[str] | None:
        """Tool names declared in the manifest front matter.

        None  = no `mcp_requirements` block at all → A8 not applicable.
        set() = the block exists but declares zero tools (distinct from None).
        """
        try:
            txt = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        fm = re.match(r"^---\s*\n(.*?)\n---\s*\n", txt, re.S)
        try:
            data = yaml.safe_load(fm.group(1) if fm else txt)
        except Exception:
            return None  # unrecognised shape → `na`, never a fail
        if not isinstance(data, dict) or "mcp_requirements" not in data:
            return None
        names: set[str] = set()
        for entry in data.get("mcp_requirements") or []:
            if isinstance(entry, dict):
                names.update(str(t) for t in (entry.get("tools") or []))
            elif isinstance(entry, str):
                names.add(entry)
        return names

    def status_block_tools(fp: Path) -> set[str] | None:
        """Tool names documented inside the file's MCP section. None = no such section."""
        try:
            content = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        m = section_re.search(content)
        if not m:
            return None
        rest = content[m.end():]
        end_m = re.search(r"^#{2,3}\s+", rest, re.MULTILINE)
        section = rest[: end_m.start()] if end_m else rest
        return set(tool_token_re.findall(section))

    reports: list[dict[str, Any]] = []
    fails: list[dict[str, Any]] = []
    # v1.49.66: `manifests` are git-diff names → repo-root relative (see git_repo_root).
    a8_root = git_repo_root(workspace)

    def _rel(p: Path) -> str:
        try:
            return str(p.relative_to(a8_root))
        except ValueError:
            return str(p)

    for m_rel in manifests:
        manifest_fp = a8_root / m_rel
        declared = manifest_tools(manifest_fp)
        if declared is None:
            reports.append({"manifest": m_rel, "verdict": "skipped_no_declaration"})
            continue
        # Look for sibling status files; documented names are the UNION over them
        # (which file a skill author documents in is their choice, not the gate's).
        skill_dir = manifest_fp.parent
        status_candidates = [
            skill_dir / "seed.memory-rules.md",
            skill_dir / "README.md",
            skill_dir / "SKILL.md",
        ]
        status_hits: list[dict[str, Any]] = []
        documented: set[str] = set()
        for cand in status_candidates:
            if not cand.exists():
                continue
            tools = status_block_tools(cand)
            if tools is None:
                continue
            status_hits.append({"file": _rel(cand), "tools": sorted(tools)})
            documented |= tools
        if not status_hits:
            reports.append({
                "manifest": m_rel,
                "declared": sorted(declared),
                "verdict": "skipped_no_status_block",
            })
            continue
        missing = declared - documented
        if missing:
            fails.append({
                "manifest": m_rel,
                "undocumented": sorted(missing),
                "declared": sorted(declared),
                "searched": [h["file"] for h in status_hits],
            })
        reports.append({
            "manifest": m_rel,
            "declared": sorted(declared),
            "documented": sorted(documented),
            "status_block_lookups": status_hits,
        })
    if fails:
        undocumented = sorted({t for f in fails for t in f["undocumented"]})
        return AssertionResult(
            assertion_id="A8",
            triggered=True,
            verdict="fail",
            detail=(
                f"{len(fails)} manifest(s) declare tool(s) absent from their status "
                f"block: {', '.join(undocumented)}"
            ),
            evidence={"fails": fails, "reports": reports},
        )
    if reports and all(r.get("verdict") == "skipped_no_declaration" for r in reports):
        return AssertionResult(
            assertion_id="A8",
            triggered=True,
            verdict="na",
            detail="A8 not applicable: no manifest declares an mcp_requirements block",
            evidence={"reports": reports},
        )
    if reports and all(str(r.get("verdict", "")).startswith("skipped") for r in reports):
        return AssertionResult(
            assertion_id="A8",
            triggered=True,
            verdict="skipped",
            detail="A8 fallback: no status block found for any declaring manifest",
            evidence={"reports": reports},
        )
    checked = [r for r in reports if "documented" in r]
    return AssertionResult(
        assertion_id="A8",
        triggered=True,
        verdict="pass",
        detail=(
            f"all declared tools documented ({len(checked)} manifest(s) checked, "
            f"{sum(len(r['declared']) for r in checked)} tool(s))"
        ),
        evidence={"reports": reports},
    )


def assertion_a9(workspace: Path, change_set: dict[str, Any]) -> AssertionResult:
    """A9 — new executable scripts detected → require verified invocation."""
    if not is_git_repo(workspace):
        return AssertionResult(
            assertion_id="A9",
            triggered=False,
            verdict="deferred",
            detail="workspace is not a git repo",
        )
    added = git_diff_names(workspace, diff_filter="A")
    # Also consider untracked new files
    code, status, _ = run_git(workspace, "status", "--porcelain=v1", "-uall")
    if code == 0:
        for line in status.splitlines():
            if line.startswith("??") and len(line) >= 4:
                added.append(line[3:].strip())
    executables: list[dict[str, Any]] = []
    # v1.49.66: `added` comes from git diff / status --porcelain → repo-root relative.
    a9_root = git_repo_root(workspace)
    for rel in set(added):
        fp = a9_root / rel
        if not fp.is_file():
            continue
        reasons: list[str] = []
        # (a) Shebang
        try:
            with fp.open("rb") as f:
                head = f.read(64)
            if head.startswith(b"#!"):
                reasons.append("shebang")
        except OSError:
            pass
        # (b) +x bit
        try:
            mode = fp.stat().st_mode
            if mode & 0o111:
                reasons.append("+x mode")
        except OSError:
            pass
        # (c) conventional executable dir
        parts = Path(rel).parts
        if any(part in {"bin", "build-tools", "scripts"} for part in parts):
            # scripts/ under a package that's a "canonical script dir"; only flag if in .../bin or build-tools/bin
            if "bin" in parts or "build-tools" in parts:
                reasons.append("conventional executable path")
        if reasons:
            executables.append({"path": rel, "signals": reasons})
    if not executables:
        return AssertionResult(
            assertion_id="A9",
            triggered=False,
            verdict="na",
            detail="no newly-added executable scripts",
        )
    return AssertionResult(
        assertion_id="A9",
        triggered=True,
        verdict="manual",
        detail=f"{len(executables)} new executable script(s) — agent MUST record verified invocation before commit",
        evidence={"executables": executables},
    )


NUMERIC_CLAIM_RE = re.compile(
    r"\b(\d+)\s*(处|个|项|fixtures?|fields?|callsites?|samples?|cases?|files?|rows?|entries?)\b",
    re.IGNORECASE,
)


def assertion_a10(workspace: Path, change_set: dict[str, Any]) -> tuple[AssertionResult, AssertionResult]:
    """
    A10(a) — numeric investigation claim vs exhaustive grep ground truth (semi-auto).
    A10(b) — parser regex vs exhaustive real samples (manual — agent verifies semantics).
    """
    claims = change_set.get("numeric_claims") or []

    # A10(a) — semi-auto: run grep to get ground truth, compare with claim
    if not claims:
        a10a = AssertionResult(
            assertion_id="A10(a)",
            triggered=False,
            verdict="na",
            detail="no numeric_claims in change-set",
        )
    else:
        checks: list[dict[str, Any]] = []
        any_fail = False
        for claim in claims:
            claim_n = claim.get("claim")
            unit = claim.get("unit", "")
            search_space = claim.get("search_space")
            anchor = claim.get("anchor_pattern")
            if claim_n is None or not search_space or not anchor:
                checks.append({
                    "claim": claim,
                    "verdict": "manual",
                    "note": "incomplete claim spec (need claim + search_space + anchor_pattern)",
                })
                continue
            try:
                proc = subprocess.run(
                    ["grep", "-rnE", anchor, search_space],
                    capture_output=True,
                    text=True,
                    cwd=str(workspace),
                    timeout=60,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired) as e:
                checks.append({"claim": claim, "verdict": "deferred", "error": str(e)})
                continue
            hits = [line for line in proc.stdout.splitlines() if line.strip()]
            actual_n = len(hits)
            match = actual_n == int(claim_n)
            if not match:
                any_fail = True
            checks.append({
                "claim_n": claim_n,
                "unit": unit,
                "search_space": search_space,
                "anchor_pattern": anchor,
                "actual_n": actual_n,
                "match": match,
            })
        verdict = "fail" if any_fail else "pass"
        a10a = AssertionResult(
            assertion_id="A10(a)",
            triggered=True,
            verdict=verdict,
            detail=(
                f"{sum(1 for c in checks if c.get('match'))}/{len(checks)} numeric claims matched"
                if not any_fail
                else f"{sum(1 for c in checks if c.get('match') is False)} numeric claim(s) mismatch"
            ),
            evidence={"checks": checks},
        )

    # A10(b) — parser design: MANUAL (agent must verify parser exhaustively)
    if not is_git_repo(workspace):
        a10b = AssertionResult(
            assertion_id="A10(b)",
            triggered=False,
            verdict="deferred",
            detail="workspace is not a git repo",
        )
    else:
        diff = git_diff_content(workspace)
        parser_signals = bool(re.search(
            r"re\.compile|sed\s+.*-E|grep\s+.*-E|awk\s+BEGIN|jq\s+\.|parser|regex|正则",
            diff,
            re.IGNORECASE,
        ))
        if parser_signals:
            a10b = AssertionResult(
                assertion_id="A10(b)",
                triggered=True,
                verdict="manual",
                detail="⚠️ manual review: parser/regex design detected — agent must confirm 0 false-negative on exhaustive real samples per [C12] Covered patterns",
            )
        else:
            a10b = AssertionResult(
                assertion_id="A10(b)",
                triggered=False,
                verdict="na",
                detail="no parser/regex design signal in diff",
            )
    return a10a, a10b


IMPORT_GUARD_RE_PATTERNS = [
    re.compile(r"HAS_([A-Z_]+)\s*=\s*True"),
    re.compile(r"try:\s*\n\s*import\s+(\w+)", re.MULTILINE),
    re.compile(r"@pytest\.mark\.skipif.*ImportError"),
    re.compile(r"importlib\.util\.find_spec\(\s*[\"'](\w+)"),
]


def _module_transitive_imports(module_path: Path) -> set[str]:
    try:
        src = module_path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return set()
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.split(".")[0])
    return imports


def _load_fleet_allowlist(workspace: Path) -> set[str]:
    """Parse references/*fleet-deps-allowlist.md for section 1+2 safe modules. Best-effort.

    stdlib essentials are seeded FIRST so they stay trusted even when the allowlist file is
    absent. The file was renamed to a `[verification]` prefix in the 2026-07-16 refactor, so
    the lookup now globs `*fleet-deps-allowlist.md` to tolerate any prefix. Previously a missing
    file returned an empty set BEFORE stdlib was added → json/os etc. were flagged as unguarded
    (A11 false positive).
    """
    # Seed stdlib essentials up-front — always trusted regardless of allowlist file presence.
    #
    # `__future__` is not merely "stdlib": it is a compiler directive that CANNOT be guarded.
    # `from __future__ import annotations` must be the first statement in the file, so wrapping
    # it in try/except raises `SyntaxError: from __future__ imports must occur at the beginning
    # of the file`. Flagging it as an unguarded optional import therefore asks for something
    # impossible, and it fires on any module using the modern annotations idiom — measured on
    # AWSBMS2ServiceSkills, where every `test/build_time/check_*.py` uses it (A11 false positive).
    safe: set[str] = {
        "__future__",
        "os", "sys", "re", "json", "ast", "argparse", "subprocess", "pathlib", "typing",
        "dataclasses", "datetime", "collections", "fnmatch", "functools", "itertools",
        "unittest", "tempfile", "shutil", "logging", "io", "contextlib", "textwrap",
        "sqlite3", "hashlib", "uuid", "abc", "enum", "copy", "warnings",
    }
    # Locate the allowlist file, tolerating the `[verification]` (or any) filename prefix.
    allowlist_path = None
    for base in (workspace / "references",
                 Path(__file__).resolve().parent.parent / "references"):
        try:
            hit = next(iter(sorted(base.glob("*fleet-deps-allowlist.md"))), None)
        except OSError:
            hit = None
        if hit and hit.exists():
            allowlist_path = hit
            break
    if allowlist_path is None:
        return safe  # file absent → still return the stdlib-trusted set (not empty)
    try:
        content = allowlist_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return safe
    in_safe_section = False
    for line in content.splitlines():
        header = re.match(r"^#{1,3}\s*(.+)", line)
        if header:
            title = header.group(1).lower()
            in_safe_section = "stdlib" in title or "brazil auto" in title or "brazilauto" in title
            continue
        if in_safe_section:
            # Match `- foo` or `- foo (details)` or `| foo |`
            m = re.match(r"^\s*[-*]\s*([a-zA-Z_][\w.]*)", line)
            if m:
                safe.add(m.group(1).split(".")[0])
            m2 = re.match(r"^\|\s*([a-zA-Z_][\w.]*)\s*\|", line)
            if m2 and m2.group(1) not in {"Module", "Package"}:
                safe.add(m2.group(1).split(".")[0])
    return safe


def assertion_a11(workspace: Path, change_set: dict[str, Any]) -> AssertionResult:
    """A11 — test code guards optional imports → verify transitive imports of tested modules are all safe or guarded.

    The test-file list is derived from `git diff` + untracked files ONLY; there is no
    `change_set["test_files"]` override (one was documented for a long time but never
    implemented). To scope this assertion, name `modules_under_test` — otherwise the heuristic
    below can resolve a "module under test" to the test file itself, which on a package that
    keeps production modules under `test/` yields a permanent false fail. See
    references/[verification]change-set-schema.md § "The A11 trap".
    """
    if not is_git_repo(workspace):
        return AssertionResult(
            assertion_id="A11",
            triggered=False,
            verdict="deferred",
            detail="workspace is not a git repo",
        )
    diff_names = git_diff_names(workspace)
    # Also include untracked
    code, status, _ = run_git(workspace, "status", "--porcelain=v1", "-uall")
    if code == 0:
        for line in status.splitlines():
            if line.startswith("??") and len(line) >= 4:
                diff_names.append(line[3:].strip())
    test_files = [
        p for p in diff_names
        if re.search(r"(?:^|/)(?:test_[\w]+|[\w]+_test|tests?/[\w/]+)\.py$", p)
    ]
    if not test_files:
        return AssertionResult(
            assertion_id="A11",
            triggered=False,
            verdict="na",
            detail="no changed/added test file",
        )
    # Check if any guard pattern present
    guarded_modules: set[str] = set()
    triggered = False
    # v1.49.66: `test_files` are git-diff names → repo-root relative.
    a11_root = git_repo_root(workspace)
    for tf_rel in test_files:
        tf = a11_root / tf_rel
        if not tf.exists():
            continue
        try:
            src = tf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat in IMPORT_GUARD_RE_PATTERNS:
            for m in pat.finditer(src):
                triggered = True
                if m.groups():
                    guarded_modules.add(m.group(1).lower())
    if not triggered:
        return AssertionResult(
            assertion_id="A11",
            triggered=False,
            verdict="na",
            detail="no optional-import guard pattern in test files",
        )
    # Collect modules-under-test (either from change-set or heuristic — test_foo.py → src/**/foo.py)
    targets = change_set.get("modules_under_test") or []
    resolved_targets: list[Path] = []
    for t in targets:
        p = (workspace / t) if not os.path.isabs(t) else Path(t)
        if p.exists():
            resolved_targets.append(p)
    if not resolved_targets:
        # Heuristic fallback: find same-name .py in sibling src/
        for tf_rel in test_files:
            tf_stem = Path(tf_rel).stem
            base = tf_stem.replace("test_", "").replace("_test", "").replace("tests_", "")
            for cand in workspace.rglob(f"{base}.py"):
                if "test" not in cand.name.lower():
                    resolved_targets.append(cand)
                    break
    if not resolved_targets:
        return AssertionResult(
            assertion_id="A11",
            triggered=True,
            verdict="deferred",
            detail="⚠️ deferred: guard pattern present but could not resolve modules_under_test",
        )
    safe_deps = _load_fleet_allowlist(workspace)
    unguarded: list[dict[str, Any]] = []
    all_ok: list[dict[str, Any]] = []
    for tgt in resolved_targets:
        imports = _module_transitive_imports(tgt)
        target_unguarded = []
        for mod in imports:
            mod_lower = mod.lower()
            if mod in safe_deps or mod_lower in safe_deps:
                continue
            if mod_lower in guarded_modules:
                continue
            target_unguarded.append(mod)
        if target_unguarded:
            unguarded.append({
                "module": str(tgt.relative_to(workspace) if tgt.is_relative_to(workspace) else tgt),
                "unguarded_imports": sorted(target_unguarded),
            })
        else:
            all_ok.append({
                "module": str(tgt.relative_to(workspace) if tgt.is_relative_to(workspace) else tgt),
                "imports_verified": sorted(imports),
            })
    if unguarded:
        return AssertionResult(
            assertion_id="A11",
            triggered=True,
            verdict="fail",
            detail=f"{len(unguarded)} module(s) have unguarded optional import(s)",
            evidence={"unguarded": unguarded, "guarded_in_test": sorted(guarded_modules)},
        )
    return AssertionResult(
        assertion_id="A11",
        triggered=True,
        verdict="pass",
        detail=f"{len(all_ok)} module(s) — all transitive imports are fleet-safe or guarded",
        evidence={"passes": all_ok, "guarded_in_test": sorted(guarded_modules)},
    )


TABLE_ROW_RE = re.compile(r"^\s*\|")


def _table_col_counts(lines: list[str]) -> list[int]:
    counts: list[int] = []
    for line in lines:
        if not TABLE_ROW_RE.match(line):
            continue
        cells = re.split(r"(?<!\\)\|", line.strip())
        # GFM rows are wrapped in outer `|` → split yields exactly one empty element at
        # each end; strip only those two wrapping artifacts with a single `if` per side.
        # Do NOT greedily pop legitimate empty cells (e.g. a blank trailing column),
        # else `| a | b | |` (3 cols, last legitimately empty) is miscounted as 2.
        if cells and cells[0].strip() == "":
            cells.pop(0)
        if cells and cells[-1].strip() == "":
            cells.pop()
        counts.append(len(cells))
    return counts


def assertion_a12(workspace: Path, change_set: dict[str, Any]) -> tuple[AssertionResult, AssertionResult, AssertionResult]:
    """
    A12(a) — table column consistency after adding a column (auto).
    A12(b) — new counting/report field invariants (manual).
    A12(c) — new parser/filter/split predicate ordering vs existing filter (manual).
    """
    if not is_git_repo(workspace):
        deferred = AssertionResult(
            assertion_id="A12(a)",
            triggered=False,
            verdict="deferred",
            detail="workspace is not a git repo",
        )
        return (
            deferred,
            AssertionResult(assertion_id="A12(b)", triggered=False, verdict="deferred", detail="no git"),
            AssertionResult(assertion_id="A12(c)", triggered=False, verdict="deferred", detail="no git"),
        )
    changed = git_diff_names(workspace)
    code, status, _ = run_git(workspace, "status", "--porcelain=v1", "-uall")
    if code == 0:
        for line in status.splitlines():
            if line.startswith("??") and len(line) >= 4:
                changed.append(line[3:].strip())

    # A12(a) — for every changed .md file, check every markdown table has consistent column count.
    md_files = [p for p in changed if p.endswith(".md")]
    a12a_reports: list[dict[str, Any]] = []
    a12a_fails: list[dict[str, Any]] = []
    # v1.49.66: `changed` are git-diff names → repo-root relative.
    a12_root = git_repo_root(workspace)
    for rel in md_files:
        fp = a12_root / rel
        if not fp.exists():
            continue
        try:
            lines = fp.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        # Segment tables (contiguous blocks of `|`-starting lines)
        i = 0
        table_idx = 0
        while i < len(lines):
            if TABLE_ROW_RE.match(lines[i]):
                j = i
                while j < len(lines) and TABLE_ROW_RE.match(lines[j]):
                    j += 1
                table_lines = lines[i:j]
                if len(table_lines) >= 2:  # at least header + separator
                    counts = _table_col_counts(table_lines)
                    if len(set(counts)) > 1:
                        a12a_fails.append({
                            "file": rel,
                            "table_line_start": i + 1,
                            "table_line_end": j,
                            "col_counts": counts,
                        })
                    else:
                        a12a_reports.append({
                            "file": rel,
                            "table_idx": table_idx,
                            "cols": counts[0] if counts else 0,
                            "rows": len(counts),
                        })
                    table_idx += 1
                i = j
            else:
                i += 1
    if not md_files:
        a12a = AssertionResult(
            assertion_id="A12(a)",
            triggered=False,
            verdict="na",
            detail="no markdown file changed",
        )
    elif a12a_fails:
        a12a = AssertionResult(
            assertion_id="A12(a)",
            triggered=True,
            verdict="fail",
            detail=f"{len(a12a_fails)} markdown table(s) have inconsistent column counts",
            evidence={"fails": a12a_fails, "verified_tables": a12a_reports},
        )
    else:
        a12a = AssertionResult(
            assertion_id="A12(a)",
            triggered=True,
            verdict="pass",
            detail=f"{len(a12a_reports)} markdown table(s) verified with consistent column counts",
            evidence={"reports": a12a_reports[:20]},
        )

    diff = git_diff_content(workspace)
    # A12(b) — new counting/report field
    b_signal = bool(re.search(
        r"[+][^+].*(_total|_count|_passed|_failed|_skipped)\s*[=:]",
        diff,
    ))
    if b_signal:
        a12b = AssertionResult(
            assertion_id="A12(b)",
            triggered=True,
            verdict="manual",
            detail="⚠️ manual review: new counting/report field detected — agent must verify invariant (e.g. total == passed + failed + skipped) under empty/all-skip/filtered scenarios",
        )
    else:
        a12b = AssertionResult(
            assertion_id="A12(b)",
            triggered=False,
            verdict="na",
            detail="no new counting field detected",
        )

    # A12(c) — new parser/filter/split
    c_signal = bool(re.search(
        r"[+][^+].*(\[[\w\s]+for\s+\w+\s+in\s+[\w()]+\s+if\s+|\.split\(|re\.split|re\.match|re\.search)",
        diff,
    ))
    if c_signal:
        a12c = AssertionResult(
            assertion_id="A12(c)",
            triggered=True,
            verdict="manual",
            detail="⚠️ manual review: new parser/filter/split predicate detected — agent must verify order vs existing filter is consistent under filtered-run scenario",
        )
    else:
        a12c = AssertionResult(
            assertion_id="A12(c)",
            triggered=False,
            verdict="na",
            detail="no new parser/filter/split detected",
        )
    return a12a, a12b, a12c


# ---------------------------------------------------------------------------
# A13 — cross-boundary fixture provenance (v1.49.72)
# ---------------------------------------------------------------------------

# Boundary vocabulary, LANGUAGE-SCOPED on purpose. `.send(` / `fetch(` in a .py file is almost
# always something else (subprocess, an internal client), so cross-applying the JS vocabulary is
# pure noise. Listed verbatim in references/[verification+revision]blast-radius-assertions.md
# § "A13 …" and pinned both ways by tests/test_run_assertions.py::test_a13_boundary_vocab_matches_spec.
#
# The keys are language NAMES, not suffix lists: a key like `.ts/.tsx/.js/.jsx` reads as a path
# token to A4's new-path-reference scan, producing a permanent false `fail` that every future CR
# touching this file would have to re-attribute. File suffixes live in the two *_SUFFIXES tuples.
A13_BOUNDARY_VOCAB: dict[str, list[str]] = {
    "javascript": ["*Accessor", "*Client", "LambdaClient", ".send(", ".invoke(", "fetch(", "axios", "nock"],
    "python": ["patch.object", "responses.add(", "requests_mock"],
}
A13_PY_BOUNDARY_METHODS: list[str] = [
    "get_json", "get", "post", "put", "delete", "request", "invoke", "send",
]

A13_PROVENANCE_MARKER = "@contract-fixture"
A13_PROVENANCE_WINDOW = 10  # lines above AND below the hit (待确认 #3)

_A13_JS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts")

# A test file, in either ecosystem. Deliberately broader than A11's Python-only pattern: the
# specimen that motivated A13 is a TypeScript `*.test.ts`.
_A13_TEST_FILE_RE = re.compile(
    r"(?:\.(?:test|spec)\.[cm]?[jt]sx?$)"      # foo.test.ts / foo.spec.tsx
    r"|(?:(?:^|/)__tests__/)"                  # __tests__/
    r"|(?:(?:^|/)(?:test_[\w]+|[\w]+_test)\.py$)"   # test_foo.py / foo_test.py
    r"|(?:(?:^|/)tests?/)"                     # test/ or tests/
)

# `*Once` chain / python side_effect list — the (b) driver signal.
_A13_ONCE_CHAIN_RE = re.compile(r"\w+(?:ResolvedValue|RejectedValue|ReturnValue|Value)Once\s*\(|side_effect\s*=\s*\[")

# A diff line that is nothing but one literal collection element followed by a comma. Low
# false-positive signal that a literal set gained/lost members.
_A13_LITERAL_ELEMENT_RE = re.compile(
    r"^[+-]\s*(?:\"[^\"]*\"|'[^']*'|`[^`]*`|[A-Za-z_][\w.]*(?:\([^()]*\))?)\s*,\s*(?:(?://|#).*)?$"
)


def _a13_vocab_regex(tokens: list[str]) -> re.Pattern[str]:
    """Compile the display tokens into one matcher, so the token list is the single source of truth.

    Three token shapes, each with its own meaning:
      `*Suffix`  → any identifier ending in that suffix (`SecurityScanAccessor`)
      `foo(`     → a call site, matched literally
      `bare`     → a bare identifier / module name, word-bounded
    """
    alts: list[str] = []
    for tok in tokens:
        if tok.startswith("*"):
            alts.append(r"\b\w*" + re.escape(tok[1:]) + r"\b")
        elif tok.endswith("("):
            alts.append(re.escape(tok))
        else:
            alts.append(r"\b" + re.escape(tok) + r"\b")
    return re.compile("|".join(alts))


def _a13_multiline_string_lines(py_src: str) -> set[int]:
    """1-based line numbers occupied by a **multi-line** string constant in a .py source.

    Why this exists: a mock detector's own test corpus necessarily pastes sample code in as a
    string (this very file's pytest does). Treating that data as a call site is a guaranteed
    false positive. Only STRICTLY multi-line constants are excluded — `patch.object(m, "get")`
    holds a single-line constant and stays visible, so the assertion loses no real coverage.
    """
    try:
        tree = ast.parse(py_src)
    except SyntaxError:
        return set()
    occupied: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            start = getattr(node, "lineno", None)
            end = getattr(node, "end_lineno", None)
            if start is None or end is None or end <= start:
                continue  # single-line constant → real code, keep it
            occupied.update(range(start, end + 1))
    return occupied


def _a13_changed_test_files(workspace: Path) -> list[str]:
    names = git_diff_names(workspace)
    code, status, _ = run_git(workspace, "status", "--porcelain=v1", "-uall")
    if code == 0:
        for line in status.splitlines():
            if line.startswith("??") and len(line) >= 4:
                names.append(line[3:].strip())
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n in seen:
            continue
        seen.add(n)
        if _A13_TEST_FILE_RE.search(n):
            out.append(n)
    return out


def assertion_a13(
    workspace: Path, change_set: dict[str, Any]
) -> tuple[AssertionResult, AssertionResult]:
    """
    A13(a) — a cross-boundary response mock must cite where its SHAPE came from (manual).
    A13(b) — literal collection size vs `*Once` chain length must be re-derived (manual).

    Spec + rationale + the measured specimen:
    references/[verification+revision]blast-radius-assertions.md § "A13 跨边界 fixture 的来源必须可引".
    """
    if not is_git_repo(workspace):
        return (
            AssertionResult(
                assertion_id="A13(a)",
                triggered=False,
                verdict="deferred",
                detail="workspace is not a git repo",
            ),
            AssertionResult(
                assertion_id="A13(b)",
                triggered=False,
                verdict="deferred",
                detail="workspace is not a git repo",
            ),
        )

    test_files = _a13_changed_test_files(workspace)
    root = git_repo_root(workspace)  # git diff names are repo-root relative (v1.49.66)

    js_re = _a13_vocab_regex(A13_BOUNDARY_VOCAB["javascript"])
    py_method_alt = "|".join(re.escape(m) for m in A13_PY_BOUNDARY_METHODS)
    py_patch_re = re.compile(r"patch(?:\.object)?\s*\([^)]*[\"'](?:" + py_method_alt + r")[\"']")
    py_other_re = _a13_vocab_regex(
        [t for t in A13_BOUNDARY_VOCAB["python"] if t != "patch.object"]
    )

    cited: list[dict[str, Any]] = []
    uncited: list[dict[str, Any]] = []

    for rel in test_files:
        fp = root / rel
        if not fp.exists():
            continue
        try:
            src = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lines = src.splitlines()
        suffix = Path(rel).suffix
        if suffix == ".py":
            skip_lines = _a13_multiline_string_lines(src)

            def _hit(line: str) -> bool:
                return bool(py_patch_re.search(line) or py_other_re.search(line))
        elif suffix in _A13_JS_SUFFIXES:
            skip_lines = set()

            def _hit(line: str) -> bool:
                return bool(js_re.search(line))
        else:
            continue  # language-scoped: no vocabulary defined for this suffix

        for idx, line in enumerate(lines, 1):
            if idx in skip_lines or not _hit(line):
                continue
            lo = max(0, idx - 1 - A13_PROVENANCE_WINDOW)
            hi = min(len(lines), idx + A13_PROVENANCE_WINDOW)
            window = "\n".join(lines[lo:hi])
            record = {"file": rel, "line": idx, "snippet": line.strip()[:160]}
            if A13_PROVENANCE_MARKER in window:
                cited.append(record)
            else:
                uncited.append(record)

    if not test_files:
        a13a = AssertionResult(
            assertion_id="A13(a)",
            triggered=False,
            verdict="na",
            detail="no changed/added test file",
        )
    elif not cited and not uncited:
        a13a = AssertionResult(
            assertion_id="A13(a)",
            triggered=False,
            verdict="na",
            detail="no cross-boundary mock in changed test files",
            evidence={"scanned_test_files": test_files},
        )
    elif uncited:
        a13a = AssertionResult(
            assertion_id="A13(a)",
            triggered=True,
            verdict="manual",
            detail=(
                f"⚠️ manual review: {len(uncited)} cross-boundary fixture(s) without a "
                f"`{A13_PROVENANCE_MARKER}` citation — agent must name the counterpart's "
                f"authoritative source (package + file:line / spec section / recorded sample), "
                f"or state why it does not apply. A mock you wrote cannot validate a contract "
                f"you did not."
            ),
            evidence={
                "uncited": uncited,
                "cited": cited,
                "window_lines": A13_PROVENANCE_WINDOW,
            },
        )
    else:
        a13a = AssertionResult(
            assertion_id="A13(a)",
            triggered=True,
            verdict="pass",
            detail=f"{len(cited)} cross-boundary fixture(s) all carry a {A13_PROVENANCE_MARKER} citation",
            evidence={"cited": cited},
        )

    # ---- A13(b): literal collection delta vs `*Once` chain length -------------
    diff = git_diff_content(workspace)
    element_delta_lines = [
        ln for ln in diff.splitlines()
        if _A13_LITERAL_ELEMENT_RE.match(ln) and not ln.startswith(("+++", "---"))
    ]
    added_elements = sum(1 for ln in element_delta_lines if ln.startswith("+"))
    removed_elements = sum(1 for ln in element_delta_lines if ln.startswith("-"))

    once_counts: dict[str, int] = {}
    for rel in test_files:
        fp = root / rel
        if not fp.exists():
            continue
        try:
            src = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if Path(rel).suffix == ".py":
            skip = _a13_multiline_string_lines(src)
            n = sum(
                len(_A13_ONCE_CHAIN_RE.findall(line))
                for i, line in enumerate(src.splitlines(), 1)
                if i not in skip
            )
        else:
            n = len(_A13_ONCE_CHAIN_RE.findall(src))
        if n:
            once_counts[rel] = n

    if not once_counts or not element_delta_lines:
        missing = []
        if not once_counts:
            missing.append("no `*Once`/side_effect chain in changed test files")
        if not element_delta_lines:
            missing.append("no literal collection element added/removed in diff")
        a13b = AssertionResult(
            assertion_id="A13(b)",
            triggered=False,
            verdict="na",
            detail="; ".join(missing),
        )
    else:
        a13b = AssertionResult(
            assertion_id="A13(b)",
            triggered=True,
            verdict="manual",
            detail=(
                f"⚠️ manual review: literal collection changed (+{added_elements}/-{removed_elements} "
                f"element lines) while {sum(once_counts.values())} `*Once`/side_effect link(s) drive it "
                f"— agent must RE-DERIVE the chain length against the NEW set size, not carry the old "
                f"value. A short chain silently feeds every value to the wrong slot."
            ),
            evidence={
                "added_element_lines": added_elements,
                "removed_element_lines": removed_elements,
                "once_chain_counts": once_counts,
            },
        )

    return a13a, a13b


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_all(workspace: Path, change_set: dict[str, Any]) -> list[AssertionResult]:
    results: list[AssertionResult] = []
    results.append(assertion_a1(workspace, change_set))
    results.append(assertion_a2(workspace, change_set))
    results.append(assertion_a3(workspace, change_set))
    results.append(assertion_a4(workspace, change_set))
    results.append(assertion_a5())
    results.append(assertion_a6(workspace, change_set))
    results.append(assertion_a7(workspace, change_set))
    results.append(assertion_a8(workspace, change_set))
    results.append(assertion_a9(workspace, change_set))
    a10a, a10b = assertion_a10(workspace, change_set)
    results.append(a10a)
    results.append(a10b)
    results.append(assertion_a11(workspace, change_set))
    a12a, a12b, a12c = assertion_a12(workspace, change_set)
    results.append(a12a)
    results.append(a12b)
    results.append(a12c)
    a13a, a13b = assertion_a13(workspace, change_set)
    results.append(a13a)
    results.append(a13b)
    return results


def summarize(results: list[AssertionResult]) -> dict[str, Any]:
    counts = {"auto_pass": 0, "auto_fail": 0, "manual": 0, "na": 0, "deferred": 0, "skipped": 0}
    for r in results:
        if not r.triggered:
            if r.verdict == "na":
                counts["na"] += 1
            elif r.verdict == "deferred":
                counts["deferred"] += 1
            elif r.verdict == "skipped":
                counts["skipped"] += 1
            else:
                counts["na"] += 1
            continue
        if r.verdict == "pass":
            counts["auto_pass"] += 1
        elif r.verdict == "fail":
            counts["auto_fail"] += 1
        elif r.verdict == "manual":
            counts["manual"] += 1
        elif r.verdict == "deferred":
            counts["deferred"] += 1
        elif r.verdict == "skipped":
            counts["skipped"] += 1
        else:
            counts["na"] += 1
    counts["any_fail"] = counts["auto_fail"] > 0
    return counts


def emit_md(results: list[AssertionResult]) -> str:
    lines: list[str] = ["### [D10]: Assertions", ""]
    must_run_ids = {"A1", "A2", "A3"}
    must_run: list[AssertionResult] = []
    conditional: list[AssertionResult] = []
    for r in results:
        base_id = r.assertion_id.split("(")[0]
        if base_id in must_run_ids:
            must_run.append(r)
        else:
            conditional.append(r)
    verdict_emoji = {
        "pass": "✅",
        "fail": "❌",
        "manual": "⚠️",
        "na": "—",
        "deferred": "⏸️",
        "skipped": "⏭️",
    }
    lines.append("**必跑:**")
    lines.append("| ID | 结果 | 详情 |")
    lines.append("|----|------|------|")
    for r in must_run:
        lines.append(f"| {r.assertion_id} | {verdict_emoji.get(r.verdict, '?')} {r.verdict} | {r.detail} |")
    lines.append("")
    lines.append("**条件:**")
    lines.append("| ID | 触发？ | 结果 | 详情 |")
    lines.append("|----|--------|------|------|")
    for r in conditional:
        triggered_mark = "✅ 是" if r.triggered else "❌ 否"
        lines.append(f"| {r.assertion_id} | {triggered_mark} | {verdict_emoji.get(r.verdict, '?')} {r.verdict} | {r.detail} |")
    return "\n".join(lines)


def emit_json(
    workspace: Path,
    change_set: dict[str, Any],
    results: list[AssertionResult],
) -> str:
    payload = {
        "schema_version": "1.0",
        "workspace": str(workspace.resolve()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "change_set": change_set,
        "assertions": [asdict(r) for r in results],
        "summary": summarize(results),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Execute [D10] Blast Radius Assertions A1-A13 (static-decidable subset).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--workspace", required=True, help="Absolute path of the workspace to check")
    parser.add_argument("--change-set", default=None, help="Path to a JSON file describing plan change-set (optional)")
    parser.add_argument("--format", choices=["md", "json"], default="md", help="Output format (default: md)")
    parser.add_argument("--output", default=None, help="Write output to file (default: stdout)")
    parser.add_argument("--diff-since", default=None, help="(Reserved) Compute diff since given ref instead of HEAD")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.exists():
        print(f"error: workspace does not exist: {workspace}", file=sys.stderr)
        return 2

    change_set_path = Path(args.change_set).expanduser().resolve() if args.change_set else None
    change_set = load_change_set(change_set_path)

    results = run_all(workspace, change_set)
    if args.format == "md":
        output = emit_md(results)
    else:
        output = emit_json(workspace, change_set, results)

    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    else:
        print(output)

    summary = summarize(results)
    return 1 if summary["any_fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

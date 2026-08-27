#!/usr/bin/env python3
"""
taxonomy.py — shared derivation engine for the loop framework's project taxonomy.

Single source of truth = push/data/project-taxonomy.yaml. This module loads it and
exposes the two derivations the framework needs:

  derive_scope(slug, workspace, tags_raw, explicit_scope=None, apply_explicit=True)
      -> (scope, reason)     # mirrors the historical plan/scripts determine_scope()
  derive_tags(packages, workspace)
      -> (tags:list, unknown:list)   # package/workspace glob -> ordered, deduped tags
  default_room() -> str
  tests_exemption_category(path) -> str | None   # project-coupled path judgments only

Design: the ALGORITHM here is a faithful copy of the original hardcoded determine_scope
(same priority order, same re.search semantics); only the PATTERNS are sourced from the
yaml. This keeps behavior identical while making the taxonomy swappable via config.

CLI (used by SKILL.md instructions for deterministic derivation):
  python3 taxonomy.py scope  --slug S [--workspace W] [--tags T] [--explicit-scope X]
  python3 taxonomy.py tags   [--package P ...] [--workspace W]
  python3 taxonomy.py room
  python3 taxonomy.py exemption --path P
  python3 taxonomy.py executor --workspace W [--executor E]
All CLI subcommands accept --format {text,json} (default text).
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

# Config path: this file lives in push/scripts/, config in push/data/.
_DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "data" / "project-taxonomy.yaml"


class TaxonomyError(RuntimeError):
    """Raised when the taxonomy config is missing or malformed (framework should
    graceful-degrade / ask the user, not crash on this)."""


def load_taxonomy(config_path: Optional[Path] = None) -> dict:
    """Load + minimally validate project-taxonomy.yaml. Raises TaxonomyError on any
    problem so callers can degrade gracefully."""
    p = Path(config_path) if config_path else _DEFAULT_CONFIG
    if not p.is_file():
        raise TaxonomyError(f"taxonomy config not found: {p}")
    try:
        import yaml  # lazy import so a missing yaml only bites config-driven paths
    except ImportError as e:
        raise TaxonomyError(f"PyYAML unavailable: {e}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise TaxonomyError(f"failed to parse {p}: {e}")
    if not isinstance(data, dict):
        raise TaxonomyError(f"{p}: top-level must be a mapping")
    for req in ("version", "package_map", "scope_rules", "default_room", "tag_order"):
        if req not in data:
            raise TaxonomyError(f"{p}: missing required key '{req}'")
    return data


# ── Scope derivation (mirrors determine_scope priority) ───────────────────────

def _match_workspace_rule(rule: dict, ws: str) -> bool:
    t = rule.get("type")
    if t == "contains":
        return rule["value"] in ws
    if t == "regex":
        return re.search(rule["value"], ws) is not None
    if t == "kiro_infra":
        home_kiro = str(Path.home() / ".kiro")
        return ws.startswith(home_kiro + "/") and "/skills/" not in ws
    raise TaxonomyError(f"unknown workspace rule type: {t!r}")


def derive_scope(
    slug: str,
    workspace: str = "",
    tags_raw: str = "",
    explicit_scope: Optional[str] = None,
    *,
    apply_explicit: bool = True,
    tax: Optional[dict] = None,
) -> tuple[str, str]:
    """Return (scope, reason). Faithful to the historical determine_scope() priority:
      1. explicit frontmatter scope (only if apply_explicit)
      2. tag_signals (case-insensitive regex)
      3. strong_slug
      4. workspace heuristics
      5. weak_slug
      6. fallback

    The parity gate calls with apply_explicit=False to test whether rules 2-6 reproduce
    the recorded scope (rather than trivially echoing the explicit field)."""
    tax = tax or load_taxonomy()
    rules = tax["scope_rules"]

    if apply_explicit and explicit_scope:
        return explicit_scope, "frontmatter:scope"

    slug = slug or ""
    workspace = workspace or ""
    tags_raw = tags_raw or ""

    for r in rules.get("tag_signals", []):
        if tags_raw and re.search(r["pattern"], tags_raw, re.IGNORECASE):
            return r["scope"], f"tags:{tags_raw}"
    for r in rules.get("strong_slug", []):
        if re.search(r["pattern"], slug):
            return r["scope"], f"slug:{slug} (strong)"
    for r in rules.get("workspace", []):
        if workspace and _match_workspace_rule(r, workspace):
            return r["scope"], f"workspace:{workspace}"
    for r in rules.get("weak_slug", []):
        if re.search(r["pattern"], slug):
            return r["scope"], f"slug:{slug}"
    return rules.get("fallback", "misc"), "fallback"


# ── Tag derivation (package/workspace glob → ordered tags) ────────────────────

def _match_package(pkg_path: str, match_glob: str) -> bool:
    """Match a package path against a glob, testing basename + full path + ~-expanded
    full path (so package-name globs like 'GFXTech*Assets' and path globs like
    '*/.kiro/skills/*' both work)."""
    candidates = {pkg_path, os.path.basename(pkg_path.rstrip("/")), os.path.expanduser(pkg_path)}
    return any(fnmatch.fnmatch(c, match_glob) for c in candidates if c)


def _tag_for_path(pkg_path: str, tax: dict) -> Optional[str]:
    for rule in tax["package_map"]:
        if _match_package(pkg_path, rule["match"]):
            t = rule["tag"]
            # tag may be a single str or a list (a package can map to multiple tags)
            return list(t) if isinstance(t, list) else [t]
    return None


def derive_tags(
    packages: Optional[list[str]] = None,
    workspace: str = "",
    *,
    tax: Optional[dict] = None,
) -> tuple[list[str], list[str]]:
    """Return (ordered_deduped_tags, unknown_paths). Uses packages[] if given, else the
    workspace path. Paths not matching any package_map rule land in `unknown` (caller
    must ask the user — never guess/omit a tag)."""
    tax = tax or load_taxonomy()
    order = tax["tag_order"]
    paths = list(packages) if packages else ([workspace] if workspace else [])
    tags: list[str] = []
    unknown: list[str] = []
    for p in paths:
        if not p:
            continue
        t = _tag_for_path(p, tax)   # None (unknown) or a list of tags
        if not t:
            unknown.append(p)
        else:
            for one in t:
                if one not in tags:
                    tags.append(one)
    tags.sort(key=lambda t: order.index(t) if t in order else len(order))
    return tags, unknown


# ── Executor derivation (pluggable executor-branch architecture) ──────────────

def _executor_default(tax: dict) -> str:
    """The fallback executor id (entry with `default: true`, else literal 'ship-check')."""
    execs = tax.get("executors", {}) or {}
    for eid, spec in execs.items():
        if isinstance(spec, dict) and spec.get("default"):
            return eid
    return "ship-check"


def _match_executor_rule(rule: dict, ws: str) -> bool:
    t = rule.get("type")
    if t == "contains":
        return rule["value"] in ws
    if t == "regex":
        return re.search(rule["value"], ws) is not None
    raise TaxonomyError(f"unknown executor resolve rule type: {t!r}")


def derive_executor(
    workspace: str = "",
    *,
    explicit_executor: Optional[str] = None,
    tax: Optional[dict] = None,
) -> tuple[str, str]:
    """Return (executor_id, reason). Resolution priority:
      1. explicit frontmatter `executor:` (if a known executor id)
      2. first executor whose `resolve` rules match the workspace path (first match wins)
      3. default executor (`default: true`, else 'ship-check')

    Missing `executors:` block or no match → default. Never raises on missing config
    (the whole point is that legacy plans with no executor concept resolve to ship-check)."""
    tax = tax or load_taxonomy()
    execs = tax.get("executors", {}) or {}
    ws = workspace or ""

    if explicit_executor and explicit_executor in execs:
        return explicit_executor, f"frontmatter:executor={explicit_executor}"

    for eid, spec in execs.items():
        if not isinstance(spec, dict):
            continue
        for rule in spec.get("resolve", []) or []:
            if ws and _match_executor_rule(rule, ws):
                return eid, f"workspace:{ws} → {rule.get('type')}:{rule.get('value')}"

    d = _executor_default(tax)
    return d, "default"


def default_room(tax: Optional[dict] = None) -> str:
    tax = tax or load_taxonomy()
    return tax["default_room"]


def tests_exemption_category(path: str, tax: Optional[dict] = None) -> Optional[str]:
    """Project-coupled path → tests_exemption category (generic rules stay in SKILL.md)."""
    tax = tax or load_taxonomy()
    for rule in tax.get("tests_exemption_paths", []):
        t = rule.get("type")
        if t == "contains" and rule["value"] in path:
            return rule["category"]
        if t == "regex" and re.search(rule["value"], path):
            return rule["category"]
    return None


# ── CLI ───────────────────────────────────────────────────────────────────────

def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Project taxonomy derivation engine")
    ap.add_argument("--config", default=None, help="override config path")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("scope", help="derive scope")
    ps.add_argument("--slug", default="")
    ps.add_argument("--workspace", default="")
    ps.add_argument("--tags", default="")
    ps.add_argument("--explicit-scope", default=None)
    ps.add_argument("--no-explicit", action="store_true", help="ignore explicit scope (parity mode)")
    ps.add_argument("--format", choices=["text", "json"], default="text")

    pt = sub.add_parser("tags", help="derive tags")
    pt.add_argument("--package", action="append", default=[], help="repeatable")
    pt.add_argument("--workspace", default="")
    pt.add_argument("--format", choices=["text", "json"], default="text")

    pr = sub.add_parser("room", help="print default room")
    pr.add_argument("--format", choices=["text", "json"], default="text")

    pe = sub.add_parser("exemption", help="tests_exemption category for a path")
    pe.add_argument("--path", required=True)
    pe.add_argument("--format", choices=["text", "json"], default="text")

    px = sub.add_parser("executor", help="resolve executor (ship-check / ux / ...) for a workspace")
    px.add_argument("--workspace", default="")
    px.add_argument("--executor", default=None, help="explicit frontmatter executor (highest priority)")
    px.add_argument("--format", choices=["text", "json"], default="text")

    args = ap.parse_args(argv)
    try:
        tax = load_taxonomy(args.config)
    except TaxonomyError as e:
        print(f"❌ taxonomy config error: {e}", file=sys.stderr)
        return 2

    if args.cmd == "scope":
        scope, reason = derive_scope(
            args.slug, args.workspace, args.tags, args.explicit_scope,
            apply_explicit=not args.no_explicit, tax=tax,
        )
        if args.format == "json":
            print(json.dumps({"scope": scope, "reason": reason}))
        else:
            print(f"{scope}  ({reason})")
    elif args.cmd == "tags":
        tags, unknown = derive_tags(args.package, args.workspace, tax=tax)
        if args.format == "json":
            print(json.dumps({"tags": tags, "unknown": unknown}))
        else:
            print("".join(f"[{t}]" for t in tags) or "(no tags)")
            if unknown:
                print(f"⚠️ unknown (ask user): {unknown}", file=sys.stderr)
    elif args.cmd == "room":
        print(default_room(tax))
    elif args.cmd == "exemption":
        cat = tests_exemption_category(args.path, tax)
        if args.format == "json":
            print(json.dumps({"category": cat}))
        else:
            print(cat or "(none — use generic SKILL.md rules)")
    elif args.cmd == "executor":
        eid, reason = derive_executor(args.workspace, explicit_executor=args.executor, tax=tax)
        spec = (tax.get("executors", {}) or {}).get(eid, {}) or {}
        if args.format == "json":
            print(json.dumps({
                "executor": eid,
                "reason": reason,
                "marker": spec.get("marker"),
                "steering": spec.get("steering"),
                "branch": spec.get("branch", eid),
            }))
        else:
            print(f"{eid}  ({reason})")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

#!/usr/bin/env python3
"""analyzer-comment-classify.py — classify a CR analyzer comment as `actionable`
(auto_fix should attempt a fix) or `inherent-noise` (skip auto_fix, surface as override).

Rules live in data/analyzer-noise-rules.yaml (config-driven, add rules without code change,
mirroring push/data/project-taxonomy.yaml). Conservative default: a comment matching NO
rule is `actionable` — we never silently skip a potential real defect.

Public function `classify_comment(comment, rules)` is pure and unit-tested.

Usage:
  # classify one comment from JSON on stdin or --json
  analyzer-comment-classify.py --json '{"analyzer":"ACodeReviewADay","content":"... GARGANTUAN ...","importance":0}'
  # classify a batch (JSON list) and print rollup
  echo '[{...},{...}]' | analyzer-comment-classify.py --batch
"""
import argparse
import fnmatch
import json
import os
import sys

try:
    import yaml
except ImportError:
    yaml = None

DEFAULT_RULES = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "analyzer-noise-rules.yaml"
)


def load_rules(path: str) -> list:
    if yaml is None:
        raise RuntimeError("PyYAML unavailable; cannot load noise rules")
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    return doc.get("rules", [])


def _matches(rule: dict, comment: dict) -> bool:
    """A rule matches iff every present criterion matches (AND semantics)."""
    analyzer = str(comment.get("analyzer", ""))
    content = str(comment.get("content", ""))
    path = str(comment.get("path", "") or comment.get("location", ""))
    importance = comment.get("importance", 0)

    if "analyzer" in rule:
        if rule["analyzer"].lower() not in analyzer.lower():
            return False
    if "content_match" in rule:
        if rule["content_match"].lower() not in content.lower():
            return False
    if "path_glob" in rule:
        # fnmatch (cross-platform, per plan skill guidance); also try basename tail match
        if not (fnmatch.fnmatch(path, rule["path_glob"])
                or fnmatch.fnmatch(path, "*" + rule["path_glob"].lstrip("*"))):
            return False
    if "max_importance" in rule:
        try:
            if int(importance) > int(rule["max_importance"]):
                return False
        except (TypeError, ValueError):
            return False
    # a rule with zero criteria should never match (avoid classifying everything as noise)
    return any(k in rule for k in ("analyzer", "content_match", "path_glob", "max_importance"))


def classify_comment(comment: dict, rules: list) -> dict:
    """Pure classifier. Returns {classification, reason, rule_id}."""
    for rule in rules:
        if _matches(rule, comment):
            return {
                "classification": "inherent-noise",
                "reason": rule.get("reason", ""),
                "rule_id": rule.get("id", ""),
            }
    return {"classification": "actionable", "reason": "", "rule_id": ""}


def rollup(comments: list, rules: list) -> dict:
    """Classify a batch; report whether any actionable remains."""
    classified = []
    actionable = 0
    for c in comments:
        r = classify_comment(c, rules)
        classified.append({**c, **r})
        if r["classification"] == "actionable":
            actionable += 1
    return {
        "total": len(comments),
        "actionable": actionable,
        "inherent_noise": len(comments) - actionable,
        "all_clear_for_autofix": actionable == 0,
        "comments": classified,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", help="single comment as JSON")
    ap.add_argument("--batch", action="store_true", help="read JSON list from stdin")
    ap.add_argument("--rules", default=DEFAULT_RULES)
    args = ap.parse_args()

    rules = load_rules(args.rules)

    if args.batch:
        comments = json.load(sys.stdin)
        print(json.dumps(rollup(comments, rules), indent=2, ensure_ascii=False))
    elif args.json:
        comment = json.loads(args.json)
        print(json.dumps(classify_comment(comment, rules), indent=2, ensure_ascii=False))
    else:
        ap.error("one of --json or --batch is required")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Outputs — content a step HANDS BACK, read by the engine and passed to whoever drives it.

WHY THIS IS NOT A FACT, AND MUST NOT BECOME ONE

A fact provider answers a question the ENGINE decides with, so its values are typed scalars and
lists — that typing is what lets an operator compare them. An output is a document excerpt, and
there is no honest way to `matches_any` a document. Two different jobs, and the type system
already separates them: shoving an excerpt into a fact would break condition evaluation, and
teaching operators to compare prose would make every condition a guess.

WHY IT IS DELIBERATELY NOT LOAD-BEARING

The content is read from a path the DRIVER recorded. So if delivering it could satisfy a
criterion, the shape would be: the driver writes a file saying the work passed, the engine reads
it back, and a reader takes that for verification. An output therefore never appears in a
completion spec, and a failure to deliver one never changes whether a step closes. It hands
content back and does nothing else; the things that decide are evidence rows and derived facts.

WHERE THE PATH COMES FROM, AND WHY NOT FROM THE SPEC

From an evidence row the driver recorded, never from a path written into the spec. A spec cannot
know this run's artifact paths — they depend on the scope, on where the work happened, on the
task — and a spec that hard-coded one would be wrong on the second machine. Taking it from a
recorded row also means "what was handed back" is answerable from the ledger rather than being
something the engine invented.

A flow's OWN files are a different thing and already have a channel: the prose tier, reached by
pointer through `guide` and `topics`. This is for content that does not exist until the run does.

ABSENCE IS NOT EMPTINESS

The status is a closed set. "The file is not there", "the selector matched nothing" and "the
content is empty" are three different answers, and a channel that rendered them alike would be
the same silence this engine keeps removing elsewhere.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import prose

DELIVERED = "delivered"
NO_SOURCE_RECORDED = "no_source_recorded"
SOURCE_MISSING = "source_missing"
SELECTOR_NO_MATCH = "selector_no_match"
UNREADABLE = "unreadable"

STATUSES = (DELIVERED, NO_SOURCE_RECORDED, SOURCE_MISSING, SELECTOR_NO_MATCH, UNREADABLE)

# Enough to carry a failure summary or a section, small enough that it cannot drown a driver.
# The whole reason `guide` is a POINTER is that dumping documents on every step defeats tiering;
# this channel reintroduces that risk, so it is capped and a cap that was hit is REPORTED.
DEFAULT_MAX_BYTES = 8192


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _select(text: str, select: dict) -> tuple[str | None, dict]:
    """Apply the one declared selector. Returns (content or None, what was applied)."""
    if not select:
        return text, {"whole_file": True}
    if "anchor" in select:
        token = str(select["anchor"])
        # A heading whose text carries the token, and everything until the next same-or-shallower
        # heading. The section-finding is prose's, reused rather than rewritten: one rule for
        # "where does a section end" instead of two that can disagree.
        rx = re.compile(rf"^#{{1,6}}\s.*{re.escape(token)}", re.M)
        return prose.extract_section(text, rx), {"anchor": token}
    if "lines" in select:
        lo, hi = (int(x) for x in str(select["lines"]).split("-", 1))
        lines = text.splitlines()
        if lo > len(lines):
            return None, {"lines": f"{lo}-{hi}", "file_lines": len(lines)}
        picked = lines[lo - 1:hi]
        return "\n".join(picked) + "\n", {"lines": f"{lo}-{hi}", "file_lines": len(lines)}
    token = str(select["regex"])
    m = re.search(token, text, re.M | re.S)
    if m is None:
        return None, {"regex": token}
    # The whole match, or group 1 when the pattern declares one — a pattern that bothers to
    # capture is saying which part it means.
    return (m.group(1) if m.groups() else m.group(0)), {"regex": token}


def deliver(spec: dict, *, recorded: list[str]) -> dict:
    """Read what a step hands back. Never raises: every failure is a reported status.

    `recorded` is every value the driver recorded under the declared evidence kind, oldest first.
    The LAST one is used, and the count is reported: several rows is legitimate (a step may
    record more than one artifact) but which one was read must not be something the reader has
    to assume.
    """
    kind = spec["from_evidence"]
    base: dict = {"from": {"evidence_kind": kind, "rows": len(recorded)},
                  "select": dict(spec.get("select") or {}) or {"whole_file": True},
                  "max_bytes": int(spec.get("max_bytes") or DEFAULT_MAX_BYTES)}
    if not recorded:
        return {**base, "status": NO_SOURCE_RECORDED, "content": None,
                "detail": f"no evidence of kind {kind!r} was recorded, so there is no path to read"}
    raw_path = recorded[-1]
    base["from"]["path"] = raw_path
    path = Path(raw_path).expanduser()
    if not path.is_file():
        return {**base, "status": SOURCE_MISSING, "content": None,
                "detail": "the recorded value does not name a readable file"}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {**base, "status": UNREADABLE, "content": None,
                "detail": f"{type(exc).__name__}: {exc}"}

    picked, applied = _select(text, spec.get("select") or {})
    base["select"] = applied
    if picked is None:
        return {**base, "status": SELECTOR_NO_MATCH, "content": None,
                "detail": "the file was read but the selector matched nothing in it"}

    encoded = picked.encode("utf-8")
    cap = base["max_bytes"]
    truncated = len(encoded) > cap
    if truncated:
        # Cut on a character boundary, not a byte one: half a codepoint is not a shorter answer.
        picked = encoded[:cap].decode("utf-8", errors="ignore")
        encoded = picked.encode("utf-8")
    return {**base, "status": DELIVERED, "content": picked,
            "bytes": len(encoded), "sha256": _digest(encoded), "truncated": truncated}


def summarise(payload: dict) -> str:
    """One line for the text form. Never prints the content — that is the payload's job."""
    st = payload["status"]
    src = payload["from"].get("path") or f"(no {payload['from']['evidence_kind']} recorded)"
    if st != DELIVERED:
        return f"⃝ output {st}: {payload.get('detail', '')}  [{src}]"
    where = payload["select"]
    which = ("whole file" if where.get("whole_file")
             else f"anchor {where['anchor']!r}" if "anchor" in where
             else f"lines {where['lines']}" if "lines" in where
             else f"regex {where['regex']!r}")
    return (f"📤 output delivered: {payload['bytes']} bytes from {which}"
            + ("  ⚠️ TRUNCATED at the declared cap" if payload["truncated"] else "")
            + f"  [{src}]")

#!/usr/bin/env python3
"""Render the MEASURED facts that `integrations/WIRING.md` would otherwise restate by hand.

WHY THIS EXISTS

WIRING.md opens by declaring its own rule: it does not restate engine facts, because everything
the engine knows is produced by a command. It then broke that rule in four places and each one
aged exactly as the rule predicts — a brief line count, a judgment-rule count, a spec size, and
two adapter-contract case counts. None of them was wrong when written.

The document's prose is JUDGMENT and stays hand-written: why the hook is the only enforcement
point, why the isolation axis is a scope rather than an agent, why fail-open is the iron rule.
None of that is derivable and none of it belongs in code. What IS derivable now lives in one
generated block, so it cannot drift without a test noticing.

WHY HERE AND NOT IN THE CLI

The command surface is a contract with its own partition test and its own classification tables;
adding a subcommand for one document's benefit would widen it for a reader who is not the driver.
`integrations/` is where the artifacts for whoever is WIRING A RUNTIME already live
(`kiro-pretooluse.py`, `mcp-server.py`), and this is one of those.

  render it   : python3 integrations/render-wiring.py
  splice it in: python3 integrations/render-wiring.py --write
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "integrations" / "WIRING.md"
BEGIN = "<!-- BEGIN GENERATED — python3 integrations/render-wiring.py --write -->"
END = "<!-- END GENERATED -->"


def _run(*args: str) -> str:
    """Ask the engine, in a throwaway state dir so measuring cannot touch a real ledger."""
    env = {**os.environ, "HARNESS_STATE_DIR": str(ROOT / "build" / "render-wiring-state")}
    p = subprocess.run([sys.executable, "-m", "engine.harness", *args],
                       capture_output=True, text=True, cwd=str(ROOT), env=env)
    return p.stdout


def facts() -> dict:
    sys.path.insert(0, str(ROOT))
    from engine import brief as briefmod
    import yaml

    contract = json.loads(_run("adapter-contract"))
    portable = _run("brief", "--portable")

    # The BIGGEST installed flow, derived rather than named: the point the prose makes is about
    # scale, and a hardcoded name would be a second thing to keep in step with the tree.
    biggest, spec = None, None
    for p in sorted((ROOT / "abilities").glob("*/flow.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        if biggest is None or len(d.get("steps") or []) > len(biggest.get("steps") or []):
            biggest, spec = d, p
    steps = biggest.get("steps") or []
    prose_dir = spec.parent / "prose"
    prose_lines = sum(len(f.read_text(encoding="utf-8").splitlines())
                      for f in sorted(prose_dir.rglob("*.md"))) if prose_dir.is_dir() else 0
    directives = [str(s.get("directive") or "").strip().splitlines() for s in steps]
    directives = [d for d in directives if d]
    return {
        "brief_lines": len(portable.splitlines()),
        "judgment_rules": len(briefmod.JUDGMENT),
        "conditional_sections": len(briefmod._CONDITIONAL),
        "biggest_ability": biggest["ability"],
        "biggest_steps": len(steps),
        "spec_kb": round(spec.stat().st_size / 1024),
        "prose_lines": prose_lines,
        "directive_median_lines": sorted(len(d) for d in directives)[len(directives) // 2],
        "translation_cases": len(contract["translation"]),
        "resilience_cases": len(contract["resilience"]),
        "end_to_end_cases": len(contract["end_to_end"]),
        "default_state_dir": "$XDG_STATE_HOME/harness-engine",
        "default_state_dir_fallback": "~/.local/state/harness-engine",
    }


def block(f: dict | None = None) -> str:
    f = f or facts()
    return "\n".join([
        BEGIN,
        "",
        "| 本安装实测 | | 谁产出它 |",
        "|---|---|---|",
        f"| 驱动契约（`--portable`） | {f['brief_lines']} 行 | `harness brief --portable` |",
        f"| 其中不可派生的判断规则 | {f['judgment_rules']} 条 | `brief.JUDGMENT` |",
        f"| 只在用到时才渲染的小节 | {f['conditional_sections']} 个 | `brief._CONDITIONAL` |",
        f"| 最大的一条 flow | `{f['biggest_ability']}`，{f['biggest_steps']} 步 | "
        f"`harness abilities` |",
        f"| 它的 spec 与散文 | {f['spec_kb']} KB + {f['prose_lines']} 行 | 磁盘 |",
        f"| 一步的 directive（中位数） | {f['directive_median_lines']} 行 | 同上 |",
        f"| 适配器用例 translation | {f['translation_cases']} 条 | "
        f"`harness adapter-contract` |",
        f"| 适配器用例 resilience | {f['resilience_cases']} 条 | 同上 |",
        f"| 适配器用例 end_to_end | {f['end_to_end_cases']} 条 | 同上 |",
        f"| 状态库默认位置 | `{f['default_state_dir']}`"
        f"（未设时 `{f['default_state_dir_fallback']}`） | `harness init` 会打印实际路径 |",
        "",
        END,
    ])


def main() -> int:
    rendered = block()
    if "--write" not in sys.argv:
        print(rendered)
        return 0
    text = DOC.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        print(f"⛔ {DOC.name} has no generated block to replace. Insert these two markers "
              f"where it belongs:\n  {BEGIN}\n  {END}", file=sys.stderr)
        return 1
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    DOC.write_text(head + rendered + tail, encoding="utf-8")
    print(f"✅ rewrote the generated block in {DOC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

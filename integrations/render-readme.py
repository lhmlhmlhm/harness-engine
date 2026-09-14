#!/usr/bin/env python3
"""Render README.md's generated block: the numbers a reader would otherwise have to trust.

Why this exists at all: README carried 7 drifted numbers while `WIRING.md` — same repo, same
author, same weeks — carried 0, and the only difference between them was that WIRING.md's
derivable facts were generated and byte-guarded. The drift was not carelessness; it was the
absence of a mechanism. One of the 7 was a *quoted command output* presented as measured
evidence, and the README contradicted itself about it within 11 lines.

THE CONSTRAINT THAT SHAPES WHAT GOES IN HERE
--------------------------------------------
This engine is meant to be published with `abilities/` emptied down to a sample. So a fact is
allowed in this block only if it SURVIVES that deletion — i.e. it must come from `engine/`,
never from the set of installed flows. "15 completion predicates" survives; "8 installed
flows" and "shipcheck-asis has 102 steps" do not: after publishing they would be confidently
wrong, which is worse than absent.

That rule is not a convention to remember, it is asserted by a test
(`test_the_generated_block_holds_no_fact_that_publishing_would_falsify`), because a rule kept
only in a docstring is one refactor from gone.

Ability-derived numbers therefore do not live in the block AND do not live in the prose. They
were removed, and regex guards refuse to let them back in.

  see it:      python3 integrations/render-readme.py
  splice it in: python3 integrations/render-readme.py --write
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "README.md"
BEGIN = "<!-- BEGIN GENERATED — python3 integrations/render-readme.py --write -->"
END = "<!-- END GENERATED -->"


def _tests() -> int:
    """Ask pytest how many tests exist. Collection only — running them here would be absurd."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "--collect-only"], capture_output=True, text=True, cwd=str(ROOT), env=env)
    m = re.search(r"(\d+) tests? collected", p.stdout)
    if not m:
        raise SystemExit(f"⛔ could not read a test count from pytest:\n{p.stdout[-400:]}")
    return int(m.group(1))


def facts() -> dict:
    sys.path.insert(0, str(ROOT))
    from engine import facts as factsmod, flow, harness, operators, predicates, proof

    mods = sorted((ROOT / "engine").glob("*.py"))
    return {
        "engine_modules": len(mods),
        "engine_lines": sum(len(p.read_text(encoding="utf-8").splitlines()) for p in mods),
        "entry_lines": len((ROOT / "bin" / "harness").read_text(encoding="utf-8").splitlines()),
        "tables": len(re.findall(r"CREATE TABLE",
                                 (ROOT / "engine" / "schema.sql").read_text(encoding="utf-8"))),
        "spec_major": flow.SPEC_MAJOR,
        "predicates": len(predicates._REGISTRY),
        "witnesses": len(proof._WITNESSES),
        "providers": len(factsmod._PROVIDERS),
        "operators": len(operators._OPERATORS),
        "capabilities": len(flow.ENGINE_CAPABILITIES),
        "scope_match_modes": len(flow.SCOPE_MATCH_MODES),
        "guard_verdicts": len(harness.GUARD_VERDICTS),
        "prose_only": len(harness.PROSE_ONLY),
        "no_json_yet": len(harness.NO_JSON_YET),
        "tests": _tests(),
    }


def block(f: dict | None = None) -> str:
    f = f or facts()
    # `NO_JSON_YET` is the one row whose INTERESTING value is zero, so it says so in words.
    # A bare "0" reads like a column that was never filled in.
    nojson = "空集（每个写命令都能用 `--json` 作答）" if f["no_json_yet"] == 0 \
        else f"{f['no_json_yet']} 个命令还只有散文"
    return "\n".join([
        BEGIN,
        "",
        "**这张表是生成的**（`python3 integrations/render-readme.py --write`），且**只含引擎自己的",
        "事实** —— 没有一行来自 `abilities/` 里装了什么。把 `abilities/` 清空到只剩一个 sample，",
        "下面每个数字依然成立；这是有测试钉住的，不是习惯。",
        "",
        "| 引擎实测 | | 出处 |",
        "|---|---|---|",
        f"| 基座 | {f['engine_modules']} 个模块 · {f['engine_lines']:,} 行 | `engine/*.py` |",
        f"| 入口 | {f['entry_lines']} 行（行为全在 `engine/`） | `bin/harness` |",
        f"| 引擎自己拥有的表 | {f['tables']} 张 | `engine/schema.sql` |",
        f"| spec 格式 MAJOR | {f['spec_major']} | `flow.SPEC_MAJOR` |",
        f"| 完成谓词 | {f['predicates']} 个 | `predicates._REGISTRY` |",
        f"| gate 防伪 witness | {f['witnesses']} 个 | `proof._WITNESSES` |",
        f"| 运行时事实 provider | {f['providers']} 个 | `facts._PROVIDERS` |",
        f"| 条件操作符 | {f['operators']} 个 | `operators._OPERATORS` |",
        f"| 一条 flow 可声明的引擎能力 | {f['capabilities']} 项 | `flow.ENGINE_CAPABILITIES` |",
        f"| `scope_match` 模式 | {f['scope_match_modes']} 种 | `flow.SCOPE_MATCH_MODES` |",
        f"| `guard` 裁决闭集 | {f['guard_verdicts']} 种 | `harness.GUARD_VERDICTS` |",
        f"| 只有散文的命令 | {f['prose_only']} 个（各带理由） | `harness.PROSE_ONLY` |",
        f"| 还没有 `--json` 的命令 | {nojson} | `harness.NO_JSON_YET` |",
        f"| 测试 | {f['tests']} 个 | `pytest --collect-only` |",
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

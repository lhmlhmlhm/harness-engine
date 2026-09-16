#!/usr/bin/env python3
"""Render README.md's flow.yaml field reference from the engine's own key sets.

WHY GENERATED, AND WHAT THAT CAN AND CANNOT BUY
-----------------------------------------------
The engine already owns the authoritative answer to "which keys exist": `flow.TOP_KEYS` and
`flow.STEP_KEYS`, both closed — an unknown key is exit 2. That strictness exists because a
silently-ignored key is how a spec ends up meaning something other than it reads (`gaet: affirm`
produces an UNGATED step while the yaml appears to declare a gate).

But the README carried no field reference at all. Measured before this existed: of 37 declared
keys, 7 appeared nowhere in the README and 9 appeared exactly once — listed in passing, not
explained. The prose is organised by DESIGN ARGUMENT ("five constraints and where each came
from"), so a key with no story behind it simply never got written down. `config` is the sharpest
case: a top-level key that changes runtime behaviour, documented nowhere.

GENERATION CANNOT WRITE THE MEANINGS. Names are derivable; what a key MEANS is not. So the
descriptions live here, beside the generator, and the anti-drift property is enforced the only
way it can be: **this script refuses to render if any declared key lacks a description**. Add a
key to the engine without describing it and the render fails — the same shape as the engine
refusing an undeclared key, one layer out.

That refusal is the mechanism. A test asserting the same thing is a second copy; the test that
exists instead asserts the README block AGREES with the engine, which is what a reader relies on.

THE PUBLISHING CONSTRAINT applies here too: no description may name an installed ability or a
domain noun from one, because this file is published with `abilities/` reduced to the samples.

  see it:       python3 integrations/render-spec-fields.py
  splice it in: python3 integrations/render-spec-fields.py --write
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "README.md"
BEGIN = "<!-- BEGIN GENERATED FIELDS — python3 integrations/render-spec-fields.py --write -->"
END = "<!-- END GENERATED FIELDS -->"

# 顶层字段。第二列写「不写会怎样」，因为一个字段的默认行为比它的类型更常被问到。
TOP: dict[str, tuple[str, str]] = {
    "version": ("spec 格式版本，如 `2.0`。MAJOR 变化意味着可读性声明变了，MINOR 只表示加了键。",
                "必填 —— 缺失即 exit 2"),
    "ability": ("这份 spec 的注册名。必须与所在目录同名，注册按它建命名空间。",
                "必填"),
    "title": ("一句人读的名字，出现在 `open` / `status` 的输出里。", "缺失则退回用 `ability`"),
    "role": ("这条 flow 的角色：可被路由的、只被借用的、还是测试夹具。合法值见 `flow.ROLES`。",
             "必填 —— 决定它是否出现在路由表里"),
    "when": ("一句话说明「什么时候该找这条 flow」。它是路由的唯一依据，所以缺失即 INVALID。",
             "必填 —— 缺失即 exit 2"),
    "scope_kind": ("这条 flow 作用在什么东西上（一个仓库、一份文档……）。`open --scope` 的值按它解释。",
                   "必填"),
    "scope_match": ("运行时把「自己在哪」与 run 的 scope 作比较的方式。合法值见 `flow.SCOPE_MATCH_MODES`。",
                    "默认 `exact`"),
    "uses": ("这条 flow 用到了引擎的哪些能力。**声明不全即拒绝加载** —— 用了没声明的键会被指名报错。",
             "缺失则视为一项都不用，于是任何高级键都会被拒"),
    "requires": ("它借用了哪些别的 flow 的注册（provider / witness 等）。让共享的事实源保持单一，"
                 "而不必依赖加载顺序。", "缺省为空：不借用任何东西"),
    "variants": ("这条 flow 的几种形状：`values` / `default` / 可选的 `fact`。声明了 `fact` 就在 open 时"
                 "从事实派生；**不声明则是「显式给出，否则默认」**。", "缺省为单一形状"),
    "results": ("这条 flow 允许怎样结束。`close-run --result` 的取值按它校验。",
                "缺省则引擎退回自己的兜底集"),
    "config": ("配置项的默认值（一个映射）。run 自己的配置覆盖它，合并结果供步骤与 hook 读取。",
               "缺省为空映射：所有配置项都无默认"),
    "facts": ("`providers` 列出这条 flow 要用的运行时事实来源。自己注册的写裸名，借来的写"
              "`<ability>.<provider>` 限定名。", "缺省为不读任何运行时事实"),
    "phases": ("阶段列表。每个阶段可带 `title` / `stages` / `guide` / `goal`。步骤必须落在已声明的阶段"
               "与阶段已声明的 stage 里。", "必填"),
    "steps": ("步骤列表，字段见下一张表。", "必填"),
    "exclusive_groups": ("互斥组：**组内关掉一支就满足该组，其余记为 skipped**。用来表达「同一件事的几种"
                         "做法，择一」—— 与 `variants`（该步在这个形状下根本不存在）是两件事。",
                         "缺省为无互斥组"),
    "guards": ("哪些动作要先问过引擎才能做。守的是工具调用，与步骤的完成判据无关。",
               "缺省为不拦任何动作"),
    "hooks": ("按触发条件生效的附加约束（阶段结束时的契约、派生事实成立时的义务……）。",
              "缺省为无 hook"),
    "prose": ("散文的挂载方式：目录、topic 目录、锚点写法等。指令与指南从散文解析，而不是抄进 spec。",
              "缺省则不挂载散文"),
}

# 步骤字段。
STEP: dict[str, tuple[str, str]] = {
    "id": ("步骤标识，全 flow 唯一。命令行、依赖、互斥组、provenance 都用它指认这一步。", "必填"),
    "phase": ("这一步属于哪个阶段。必须是 `phases` 里声明过的。", "必填"),
    "stage": ("阶段内的更细分组。阶段没声明这个 stage 即报错 —— 拼错会造出一个幽灵分组，"
              "而阶段目标谓词正是按它筛选的。", "缺省为不分组"),
    "title": ("一句人读的名字。", "缺失则退回用 `id`"),
    "deps": ("必须先完成的步骤。**被 skip 的步骤算完成**（互斥组正是靠这点让下游不必等一支永不会跑的分支）；"
             "但变体下**不适用**的步骤既不 closed 也不 skipped，所以依赖它会永久阻塞 —— 引擎在加载期就拒绝。",
             "缺省为无依赖"),
    "variants": ("这一步只在哪些变体下**存在**。不在列表里的变体下它不是可选，是不属于这条流程。"
                 "**列出全部变体会被拒绝** —— 那等于没有过滤，且新增变体时会静默失去保护。",
                 "缺省为在所有变体下都存在（共享核心）"),
    "gate": ("这一步需要哪种门：人的确认、预授权……合法值由引擎定义。", "缺省为无门"),
    "strict_witness": ("要求门的背书必须是强证据，不接受降级。**没有门却设置它会被拒**。", "默认 `false`"),
    "completion": ("完成判据。`type` 必填；判据类型见「完成谓词」一章。",
                   "缺省为 `{type: attest}`（只要一句自证）"),
    "directive": ("这一步要做什么的祈使句，`next` 直接把它交给执行者。", "缺省为空"),
    "guide": ("指向散文里更长的说明。指令说「做什么」，指南说「怎么做、为什么」。", "缺省为不挂指南"),
    "topics": ("这一步引用的散文 topic。让一条纪律只写一处、被多步引用。", "缺省为不引用"),
    "autonomy": ("这一步由谁来走（如执行者自己）。`next` 会把它报出来。", "默认 `agent`"),
    "optional": ("这一步可以被 `skip` 跳过，且不计入必需步骤。跳过要记录一个决定。", "默认 `false`"),
    "repeatable": ("这一步可以走多轮。证据按轮次归属，所以上一轮的失败不会挡住这一轮。", "默认 `false`"),
    "budget": ("可重复步骤的轮数上限。**不可重复的步骤设置它会被拒** —— 那是一份自相矛盾的声明。",
               "默认 0（不限）"),
    "output": ("这一步交回什么：从证据里选取，可限长。刻意不承重 —— 它是给调用方看的，不是判据。",
               "缺省为不交回结构化输出"),
    "produced_by": ("这一步的产物由哪个工具生成。相对路径按 ability 根解析，`next` 会给出绝对路径。",
                    "缺省为无声明的生产者"),
}


def _check(top_keys, step_keys) -> None:
    """Refuse to render an incomplete reference. THIS is the anti-drift mechanism.

    A missing description is not a cosmetic gap: the table would render with the key absent, and a
    reader has no way to tell an undocumented key from a nonexistent one. Extra descriptions are
    refused too — one for a key the engine no longer accepts documents a spec that would exit 2.
    """
    problems = []
    for label, declared, described in (("top-level", set(top_keys), set(TOP)),
                                       ("step", set(step_keys), set(STEP))):
        if missing := sorted(declared - described):
            problems.append(f"{label} key(s) the engine accepts but nothing here describes: {missing}")
        if extra := sorted(described - declared):
            problems.append(f"{label} key(s) described here that the engine does not accept: {extra}")
    if problems:
        raise SystemExit("⛔ field reference is out of step with the engine:\n  "
                         + "\n  ".join(problems)
                         + "\n  Describe the key in integrations/render-spec-fields.py, or remove it.")


def block() -> str:
    sys.path.insert(0, str(ROOT))
    from engine import flow
    _check(flow.TOP_KEYS, flow.STEP_KEYS)
    rows = [BEGIN, "",
            "**这两张表是生成的**（`python3 integrations/render-spec-fields.py --write`）。字段名取自",
            "引擎自己的闭集 `flow.TOP_KEYS` / `flow.STEP_KEYS` —— 加一个键而不在这里写明它的含义，",
            "渲染会直接失败，与引擎拒绝一个未声明的键是同一形状。",
            "",
            "未知键一律 exit 2 并列出合法键：**一个被静默忽略的键，就是一份 spec 读起来是一个意思、",
            "实际是另一个意思的由来。**",
            ""]
    for label, table in ((f"顶层字段（{len(TOP)}）", TOP), (f"步骤字段（{len(STEP)}）", STEP)):
        rows += [f"#### {label}", "", "| 字段 | 含义 | 不写会怎样 |", "|---|---|---|"]
        for k in sorted(table):
            desc, default = table[k]
            rows.append(f"| `{k}` | {desc} | {default} |")
        rows.append("")
    rows.append(END)
    return "\n".join(rows)


def main() -> int:
    rendered = block()
    if "--write" not in sys.argv:
        print(rendered)
        return 0
    text = DOC.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        print(f"⛔ {DOC.name} has no field block to replace. Insert these two markers where it "
              f"belongs:\n  {BEGIN}\n  {END}", file=sys.stderr)
        return 1
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    DOC.write_text(head + rendered + tail, encoding="utf-8")
    print(f"✅ rewrote the field block in {DOC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

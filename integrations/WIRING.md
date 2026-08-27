# 把 harness-engine 接到一个 kiro agent 上

三件事:一个 hook(强制)、一份操作手册(资源)、一条环境变量(必须一致)。

## 1. hook —— 唯一的强制点

```jsonc
// ~/.kiro/agents/<your-agent>.json
{
  "hooks": {
    "preToolUse": [
      { "matcher": "shell",
        "command": "python3 \"$HOME/…/harness-engine/integrations/kiro-pretooluse.py\"",
        "timeout_ms": 8000 },
      { "matcher": "TaskeiUpdateTask",
        "command": "python3 \"$HOME/…/harness-engine/integrations/kiro-pretooluse.py\"",
        "timeout_ms": 8000 }
    ]
  }
}
```

`matcher` 要覆盖**哪些工具**,取决于各 ability 声明了哪些 `guards.<action>.matches[].tool`。
列出全部:

```bash
python3 - <<'EOF'
import sys; sys.path.insert(0, "<engine>")
from engine import flow
for a in flow.available_abilities():
    f = flow.load(a)
    tools = {r["tool"] for rules in f.guard_matches.values() for r in rules}
    if tools: print(f"{a}: {', '.join(sorted(tools))}")
EOF
```

**没被 `matcher` 覆盖的工具,它的 `matches` 规则永远不会被问到。** 这是个安静的失效:
ability 里声明得好好的,而 hook 从没被调用。加新 guard 时要回来核对这份清单。

## 2. `resources` —— 放操作手册,不要放 flow.yaml

```jsonc
"resources": [
  "file://~/…/harness-engine/integrations/DRIVING.md"
]
```

**不要**把 `flow.yaml` 或 `prose/` 放进 `resources`。转写后的交付流程是 35 KB spec + 1,196 行
散文;预载它们会把整个渐进披露设计废掉——那套设计的全部意义就是 agent **不**预载,而是每步问
`harness next`,拿到 directive(约 2 行)+ guide 指针,只在需要时才 `harness show`。

`DRIVING.md` 是给 agent 读的驱动契约(退出码语义 + 调用循环),与 README(给人读的项目说明)
是两份东西。

## 3. `HARNESS_STATE_DIR` —— 两侧必须一致

引擎默认把状态库放在 `<engine>/state/harness.db`。若 agent 侧设了这个环境变量,
**hook 侧必须设同一个值**,否则 hook 查的是另一个库、永远找不到 open run、静默全放行。

适配器会显式检查并报警(而不是静默放行),因为「查不到库」和「没有 open run」在下游长得一样:

```
⚠️  HARNESS_STATE_DIR=/tmp/x has no harness.db — guard NOT enforced.
```

最省事的做法是**两边都不设**,让它用引擎目录下的默认值。

## 强制到什么程度:一个必须知道的边界

hook 强制的是「**已开 run 内**的门」。它**不能**强制「run 被开过」——`guard-tool` 在找不到
open run 时放行(fail-open 是铁律,它跑在每个会话的每次工具调用之前)。

所以「不开 run」是一条敞开的绕过路径。目前只能靠 prompt/steering 里的指令去堵,而这个项目的
论点恰恰是**散文绑不住 agent**。结构性的答案是在声明过的位置内把 fail-open 翻成 fail-closed,
但那有真实代价(一个 bug 会拦住那些位置里的所有提交),所以**没有默认打开**。

诚实的现状:**这套接线让「开了 run 之后绕过门」变得不可能,而没有让「开 run」变成不可能不做。**

## 验证接线是否真的生效

```bash
cd <一个 ability 的 scope 内>
harness open <ability> --scope "$(pwd)" --run smoke

echo '{"tool_name":"shell","tool_input":{"command":"git commit -m x"}}' \
  | python3 <engine>/integrations/kiro-pretooluse.py ; echo "exit=$?"     # 期望 2

echo '{"tool_name":"shell","tool_input":{"command":"git status"}}' \
  | python3 <engine>/integrations/kiro-pretooluse.py ; echo "exit=$?"     # 期望 0
```

拿到 `exit=0` 而期望 2 时,按这个顺序查:

| 现象 | 原因 |
|---|---|
| 有 `⚠️ no harness.db` | 两侧 `HARNESS_STATE_DIR` 不一致 |
| 有 `⚠️ NOT enforced: N open runs` | 同一 scope 多个 open run,引擎拒绝猜 |
| 无任何输出 | scope 不覆盖 cwd(检查 ability 的 `scope_match`),或该工具无匹配规则 |
| hook 根本没被调用 | agent 的 `matcher` 没覆盖这个工具名 |

## 为什么适配器只有一份,而不是每个 ability 一个

「哪个工具调用等于哪个动作」是 ability 的知识,所以它以**数据**形式住在
`guards.<action>.matches` 里:

```yaml
guards:
  commit:
    step: E04
    matches:
      - tool: shell
        field: command                     # 只看这个字段;省略则看整个序列化载荷
        pattern: '(^|[;&|]\s*)git\b[^;&|]*?\s+commit\b'
```

引擎编译并施加 pattern,**不知道 `git commit` 是什么意思**(纯净性测试会抓到它学会)。
于是适配器只做两件事:翻译退出码(引擎 4 → kiro 2),以及在任何不确定时放行。

替代做法是每个 ability 一个适配器、各自硬编码正则——那会让同一份映射存在 N 份、各自漂移,
而这正是本项目一直在反对的形态。

# 工作区隔离：判据要问「你在哪工作」，而不是「你的 scope 是什么」

## 现象

方案声明了 `worktree_isolation`，你照做了：工具开出了隔离工作树，你在里面改代码。
然后 C02b 关不掉 —— 两个合法取值同时被拒：

```
记 isolated        → 被 in_linked_worktree=false 证伪
记 shared_declared → 被 isolation_declared=true 证伪
```

## 这不是「少了个功能」，是判据在惩罚做对了的事

一次实测。工作树真实存在：

```
<worktree ability>/state/worktrees/<run>/<workspace>/src/<Pkg>
存在 True   .git 是文件 True      ← 它确实是 linked worktree
```

而 C02b 照样拒绝。原因是那条判据读的事实来自 `scope`（源仓库），不是你真正工作的那个目录。
源仓库的 `.git` 是**目录**，于是 `in_linked_worktree=false`，于是「已隔离」被判为谎话。

**一个会证伪真话的判据比没有判据更糟**：它惩罚它本来要求的那个行为，而绕过它的唯一办法
是不再说真话。

## 唯一正确的反应

**在 C01 把路径记下来。** 工具已经打印了它，且它自己的文件头就写着要持久化：

```
WORKTREE_PATH=<abs path>       <- persist THIS
```

```sh
harness evidence --run <id> --step C01 --kind worktree_path --value "<那个路径>"
```

记了之后 `worktree_state` 就去问那个目录，`isolated` 得到证实，C02b 正常关闭。

**不要**把 scope 改成工作树路径来绕过。那会把 run id 嵌进 scope，于是：scope 不再是一个
两条流程可以争用的稳定东西，而 required-flows 的前缀匹配也跟着失效。scope 该是源仓库。

## 为什么这条值得单独写一篇

因为它有三个各自都会独立复发的形状：

**① 「引擎不做的效果」缺一个生产者指针。** 隔离工作树由 `worktree-setup.sh` 开出来，引擎
只做事后只读核实 —— `providers.py` 的原话是「The engine performs none of it. The agent runs
the tool.」但在 `produced_by:` 出现之前，没有任何东西告诉 agent 该跑哪个工具。被告知「这个
效果是必须的、而且会被独立核实」，却要靠 grep 去找生产者的 agent，最后会 grep 到那个**还装着、
还能跑**的源实现 —— 于是一件工作有了两个引擎、两份进度记录，谁都不管另一个。

**② fallback 不能是静默的。** 「没记路径，所以我看了 scope」和「记了一个恰好等于 scope 的
路径」必须是两个不同的答案。所以有一个单独的事实 `worktree_path_recorded`：它让 C02b 能用
**正确的理由**拒绝 —— 不是「那个目录不是工作树」，而是「你没说清你指的是哪个目录」。

**③ 用 run id 前缀去匹配分支名是一种看不见的耦合。** `own_worktree_exists` 曾经拿
`run_id[:8]` 去 grep `refs/heads/shipcheck/*`，这要求本引擎的 run id 和源实现的 session UUID
共享前缀。实测那时它们**确实**相等 —— 靠的是安排，不是任何机制；而一旦不再相等，
`own_worktree_exists` 会永远是 false，且没有任何东西会说一句。改成问「记下的那个目录还在不在」
之后，这个耦合整条消失。

## 顺带一条

拆除同样是效果，同样有指针（E22a → `worktree-teardown.sh`）。评审还活着或工作树里有未提交
改动时，脚本会**拒绝删除并正常退出** —— 那是预期结果，不是失败。别为了「清干净」去绕它。

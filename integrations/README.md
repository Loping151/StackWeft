# 把 StackWeft 当成 subagent / skill 调用

StackWeft 是个**领域专精的交付者**，不是通用 Agent。当宿主 Agent（Claude Code、Codex
等）遇到「在真实全栈仓库里加/扩一个跨栈字段或小功能」这类活时，可以把它**整包委派**给
StackWeft：宿主只给一句自然语言需求，StackWeft 自己编译跨栈落点、填槽、跑反例探针、出
分支/PR，再把结构化结果交回宿主。对宿主来说就是一次便宜、稳定、可验证的工具调用。

调用契约只有两条命令（细节见 [`../README.md`](../README.md)、[`../bench/BENCH.md`](../bench/BENCH.md)）：

```bash
# 1) 交付：跑完整流水线；打印 run_id=…；退出码 0=通过 / 1=未过
<STACKWEFT_HOME>/sw run "给文章增加 subtitle 副标题字段，列表和详情页都展示" --repo /abs/path/to/repo

# 2) 取结构化结果（阶段 / token / 通过与否 / 分支 / PR），喂回宿主
<STACKWEFT_HOME>/sw json --debug
```

`--repo` 可省略——StackWeft 会从需求里的自然语言仓库位置自行解析（非 git 目录会自动建基线）。

## 接入各宿主

| 宿主 | 怎么装 | 文件 |
|---|---|---|
| **Claude Code** | 把 `claude-code/` 拷贝或软链到 `~/.claude/skills/stackweft-delivery/`（或项目内 `.claude/skills/…`），它会在合适场景自动被选中 | [`claude-code/SKILL.md`](claude-code/SKILL.md) |
| **Codex** | 把 `codex/stackweft.md` 的内容并进你的 `AGENTS.md`，或放进 `~/.codex/prompts/` 作为自定义提示 | [`codex/stackweft.md`](codex/stackweft.md) |

两份文件是同一套委派逻辑的不同载体——Claude Code 走 SKILL.md frontmatter 的自动触发，
Codex 走 AGENTS.md 风格的指令片段。装之前把 `<STACKWEFT_HOME>` 换成本仓库的绝对路径
（或 `export STACKWEFT_HOME=…`）。

## 何时委派 / 何时别委派

- ✅ **适合**：扩充式跨栈字段/小功能（model → 控制器/API payload → 前端 service → 列表卡片
  + 详情页 DOM），需要可验证、要省 token、同类需求会重复出现。
- ❌ **不适合**：与「字段贯穿前后端」无关的活（纯算法、运维、跨服务架构改造、不属于这种
  全栈形态的仓库）——这些留给宿主自己的通用能力，别硬塞。

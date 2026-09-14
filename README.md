# 轻舟 qingzhou

**单文件 · 零依赖 · 终端 Agent**——一个 `qingzhou.py`（约 66KB，纯 Python 标准库，不装任何包），给它任务，它自己循环干活：读文件、写文件、执行命令，直到交差。

> 轻舟已过万重山：烂电脑、U 盘、老爷机，插上就能跑。

> **v0.2（2026-09-07）**：再集六家之所长（Codex / Claude Code / Aider / Gemini CLI / OpenCode / Amp）——指令文件层级加载 + @import、lint/test 钩子（失败才注入 + 熔断）、/plan 计划模式、/undo 快照回滚、/tokens 计量、/copy-context 导出。详见下文。

## 它是什么

把各家 Agent 工具的好东西揉进一个文件：

| 来源 | 融合了什么 |
|---|---|
| Codex / Claude Code | 三档权限模式（审批 / 全自动 / 逐条），/mode 随时切 |
| Claude Code | 会话逐轮落盘（崩溃不丢）、/resume 恢复、/compact 压缩上下文 |
| smolagents / gptme | 文本协议工具调用（不挑端点，杂牌 OpenAI 兼容后端都能用） |
| DanTide | 密钥环境变量优先不落盘、退避重试、流式失败自动退化非流式 |
| 你的夜班框架 | TASKS.md 任务账本（P0-P4+三态+留档）、HANDOFF.md 交接文档 |
| agent-relay | --campaign 战役模式：班次锁 TTL 接管、STOP 收兵、进度盖章 |

## 快速开始

### 方式一：U 盘便携版（推荐给老爷机）

整个 `portable/` 文件夹拷进 U 盘，双击 `轻舟.bat` 即可。**宿主机什么都不用装**（自带 22MB 便携 Python）。

```
轻舟/
├─ 轻舟.bat          ← 双击运行
├─ qingzhou.py       ← 本体（升级就替换这个文件）
├─ python/           ← 便携 Python 3.12（embeddable）
├─ qingzhou.json     ← 配置（首次运行向导生成）
└─ sessions/         ← 会话记录（自动生成）
```

### 方式二：本机已有 Python

```bat
python qingzhou.py
```

首次运行会进入配置向导：选后端（ollama.com / LM Studio / DeepSeek / 自定义）→ 填密钥 → 选模型。也可以全走环境变量，不落盘：

```bat
set QINGZHOU_BASE_URL=https://ollama.com/v1
set QINGZHOU_API_KEY=你的key
set QINGZHOU_MODEL=glm-5.3-flash
python qingzhou.py
```

## 三档权限模式

| 档 | 行为 | 适合 |
|---|---|---|
| 1 审批档（默认） | 工作区内读写文件免确认；执行命令逐条问（可按 A 放行全部命令） | 日常 |
| 2 全自动档 | 全部放行不再询问（`--yolo` 启动） | 无人值守 / 可信任务 |
| 3 逐条档 | 每个动作都确认 | 挑剔/危险环境 |

工作区 = 启动目录。**工作区外写文件在审批档/逐条档必拦**，全自动档也会先问一次。

HELP_TEXT 会话内命令：
  /help /mode /plan /model /new /resume /compact /tokens /copy-context /undo /tasks /handoff /status /exit

## 战役模式（无人值守干活）

先把任务写进 `TASKS.md` 账本（`/tasks` 自动建模板）：

```markdown
## 待办
- [ ] P0 最优先：产出 ling.txt
- [ ] P1 搞定甲：产出 jia.txt
```

然后：

```bat
python qingzhou.py --campaign TASKS.md --rounds 10
```

轻舟会自动循环：**按 P0>P1>…领活 → 全自动干活 → 账本打勾留档 + 流水一行 → 盖章进度**，直到无任务或达到轮数。防呆三件套（源自 agent-relay 实战）：

- `--lock-ttl` 班次锁：两开不冲突，崩溃自动接管
- `.qz-stop` 收兵标记：创建这个文件，下一轮优雅结束，文件全保留
- `.qz-progress.md` 轮次盖章：每轮干了什么都有审计记录

## 常用命令速查

```bat
python qingzhou.py                       # 交互对话
python qingzhou.py --task "写个脚本"     # 单发任务，跑完退出
python qingzhou.py --yolo                # 全自动档
python qingzhou.py --resume              # 恢复上次会话
python qingzhou.py --campaign TASKS.md   # 战役模式
python qingzhou.py --no-stream           # 端点不支持流式时用
```

## 工作区里的两个约定文件

- `qingzhou.md`：项目指令（类似 AGENTS.md / CLAUDE.md），启动自动注入，写你的规矩
- `TASKS.md` / `HANDOFF.md`：任务账本与交接文档（`/tasks` `/handoff` 自动生成模板）

## 测试

```bat
python tests_smoke.py
```

39 项端到端冒烟测试（mock 端点，不需要真实 key）：核心循环、三档权限、会话落盘、协议容错、流式退化、工作区边界、战役领活回写、STOP 收兵、test 钩子全链路（失败反馈→修复→通过 + 修不好熔断）、/undo 快照、指令层级注入 + @import 展开、AGENTS.md 兼容。

## v0.2 新增机制（集六家之所长）

- **指令文件层级**（Codex AGENTS.md / Claude CLAUDE.md 式）：`~/.qingzhou/qingzhou.md` 用户级 → 从工作区向上遍历每级的 `qingzhou.md` / `AGENTS.md` / `AGENTS.local.md`，越靠近启动目录优先级越高；支持 `@路径` 引用展开（最多 3 跳，写 `` `@file` `` 可免展开）；单文件超 4MiB 跳过
- **lint/test 钩子**（Aider 式）：工作区或配置 `qingzhou.json` 里 `"hooks": {"test": "命令"}`，模型宣布完成前自动跑一次——**失败才把输出注入**让它修复（最多反馈 3 轮后熔断带病收尾），通过则零注入零浪费
- **/plan 计划模式**（Claude Code / OpenCode 式）：开启后只读调研、不动文件不跑命令，用 final_answer 交分步计划，确认后 `/plan off` 执行
- **/undo 快照回滚**（OpenCode 式）：轻舟改写已存在文件前自动快照到 `.qingzhou-undo/`，一条命令回滚最近一次改写（新建文件不受影响）
- **/tokens 计量**（Aider 式）：估算当前上下文占用，超过软上限提醒 /compact
- **/copy-context**（Aider 式）：把完整上下文导出 markdown，方便贴到外部工具复现问题
- **打断安全**（Aider 式）：Ctrl+C 打断后已产生的输出与工具动作保留在会话里，下轮直接引用

## 已知边界

- 工具协议是文本协议（```qingzhou 代码块包 JSON），极少数不守格式的模型会被连续纠正 3 次后终止——换 `--no-stream` 或换个听话点的模型
- 本机 shell 编码：Windows 控制台已自动切 UTF-8；命令输出按 UTF-8→GBK 自动探测解码
- Python 3.9+ 兼容（老爷机可换 3.9 embeddable 便携包）
- test 钩子命令跑在系统 shell，请勿配置破坏性命令；钩子失败最多反馈 3 轮即熔断收尾
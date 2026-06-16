Math-Agent Framework | 面向 AI4Math 的多智能体定理推导与验证引擎

针对偏微分方程（PDEs）、反应扩散系统等复杂长程数学推导中，大语言模型（LLM）极易产生“逻辑幻觉”和“推导跳跃”的问题，本项目开发了一套具备严格数学验证机制、物理隔离沙盒与高阶系统容错能力的学术级多智能体协同框架。

本系统不依赖纯语义生成的“盲目猜想”，而是通过底层的 SymPy 符号执行引擎进行严密的代数演算约束，确立了以“绝对数学正确性”为状态机流转依据的 AI4Math 新范式。

🌟 核心架构与特性 (Core Features)
1. 动态推导管线与符号级验证 (Symbolic Verification)
严密约束体系：强制 Teammate Agent 在生成最终 LaTeX 证明前，调用内部的 verify_math_symbolic 工具执行纯 Python/SymPy 代数演算（如验证反应扩散系统边界条件、积分分部展开）。

四步闭环协议：严格执行“文献检索 (MCP) -> 原子任务分配 -> 同行评审 (Peer Review) -> 权威合并”的工作流。主控智能体（Lead PI）作为质量网关，Reviewer Agent 负责漏洞审查（如退化边界检查、拓扑空间验证）。

2. 任务级物理隔离沙盒 (Git Worktree Sandbox)
安全生产环境：为并行的数学验证策略动态构建基于 Git Worktree 的物理隔离沙盒（create_worktree）。

跨平台兼容感知：Agent 具备宿主机环境感知能力，所有文件操作 (read_file, write_file, edit_file) 强制进行 UTF-8 编码清洗，完美规避 Windows/Linux 异构系统下的编码崩溃难题。

3. 多智能体异步通信总线 (Inbox Message Bus)
解耦式协同：抛弃脆弱的单一长对话流，建立基于本地 JSONL 的 Inbox 消息总线。支持跨智能体的异步消息传递（send_message, check_inbox）。

状态机与依赖追踪：基于 blockedBy 实现任务依赖追踪图，通过 submit_plan 与 plan_approval_response 构建严格的权限审批状态机。

4. 工业级高鲁棒性自愈引擎 (Self-Healing & Compaction)
脏数据拦截与反序列化：在底层 Hook 层实时捕获 LLM 吐出的畸形 tool_use 数据（如突变为空列表或纯字符串），并执行强制类型修正，防止解析器崩溃。

分级上下文压缩 (Context Compaction)：设计 tool_result_budget、micro_compact 与 reactive_compact 三级压缩管线。在触发 LLM max_tokens 溢出前，自动利用大模型进行历史记录的无损摘要替换，保障系统持续运行。

优雅降级网络机制：集成基于 MCP 协议的 arXiv 论文检索工具，内置动态 User-Agent 轮换与随机退避算法，精准捕获 503 限流异常并自动降级为本地文献库检索。

🛠️ 技术栈 (Tech Stack)
Core Language: Python 3.10+

LLM Engine: Anthropic Claude API (Primary) / DeepSeek (Fallback)

Mathematical Verification: SymPy

System Integration: Git Worktree, Threading (Background Tasks), Cron Scheduler

Tool Interfaces: Model Context Protocol (MCP)

🏗️ 快速开始 (Quick Start)
环境依赖
Bash
git clone https://github.com/maksymilan/math-agent-framework.git
cd math-agent-framework

# 推荐使用虚拟环境
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

pip install anthropic python-dotenv requests sympy
配置环境变量
在根目录创建 .env 文件，并填入你的 API 密钥：

代码段
ANTHROPIC_API_KEY=your_api_key_here
MODEL_ID=claude-3-5-sonnet-20241022
FALLBACK_MODEL_ID=claude-3-haiku-20240307
# 可选：配置自定义 Base URL
# ANTHROPIC_BASE_URL=https://api.your-proxy.com
启动主控引擎
Bash
python s20_comprehensive/code.py
启动后，您将在终端中看到 s20 >> 提示符。您可以输入类似以下的高阶指令：

"Please derive the invariant manifold for the Lotka-Volterra competition system with diffusion. Set up a task, verify the integration by parts using SymPy, and submit a rigorous LaTeX proof."

📂 目录结构概述
Plaintext
.
├── .tasks/               # 细粒度原子任务状态存储 (JSON)
├── .worktrees/           # Git Worktree 物理隔离沙盒
├── .mailboxes/           # Agent 间异步通信 Inbox 总线
├── skills/               # 动态加载的原子化技能库 (.md + Frontmatter)
├── Conference/           # arXiv 文献下载与解压归档
└── code.py               # 引擎核心入口（包含所有调度器与工具 Hook）

https://github.com/shareAI-lab/learn-claude-code

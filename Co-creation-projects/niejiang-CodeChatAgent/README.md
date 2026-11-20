# CodeChatAgent

基于 Hello-Agents 的聊天式代码分析助手。计划通过对话理解项目结构、查找代码，并逐步分析调用关系。

## 当前进度

已接入 Hello-Agents 和百炼兼容接口，支持命令行和本机网页多轮对话。可以直接聊天，需要分析代码时在对话中给出项目路径，助手按需打开项目并进行目录浏览、文本搜索和文件读取；一个会话可同时打开多个项目（如比较两个项目），助手根据项目简称、技术栈、README 简介和最近使用情况判断用户指的是哪个项目。工具调用使用模型接口原生的 function calling（OpenAI 兼容的 `tools` / `tool_calls`），参数以 JSON 传递并按严格 Schema 声明；参数无法解析时会把错误返回给模型重新调用。回答和工具调用过程以事件形式逐步产出：命令行逐段打印，网页通过 SSE 逐段显示并可中途停止。对话历史按上下文预算管理：每轮只保留问题、回答和一行工具摘要，超过预算一半时从最早一轮开始删除；一轮内工具结果累积到预算 60% 时，较早的结果替换为占位说明、只保留最近 3 条原文，重复的相同调用只保留最新结果；输入接近模型上限时会移除历史并缩短工具结果后重试。网页对话保存在本机 SQLite（`data/chat.db`），刷新页面或重启服务后都能恢复；命令行对话不保存。

## 启动网页聊天

在项目根目录打开两个终端。首次使用时先安装依赖：

```bash
.venv/bin/python -m pip install -r requirements.txt
cd frontend && npm install
```

第一个终端启动后端（本机如有代理配置，此命令也避免本地地址的代理解析问题）：

```bash
NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 .venv/bin/python -m uvicorn app.api.web:app --app-dir backend --host 127.0.0.1 --port 8018
```

第二个终端启动前端：

```bash
cd frontend
npm run dev
```

打开 <http://127.0.0.1:5173/> 即可直接聊天；需要分析代码时，在消息中写出项目路径，或提到已授权项目的名称。侧栏上方是历史对话（按今天、昨天、近 7 天、更早分组；点击切换，✎ 或双击标题重命名，✕ 再点“删除”永久删除），下方是可折叠的已授权项目：点击可加入当前对话（不产生新的授权），已在当前对话中打开的标有“已打开”，✕ 再点“撤销授权”可撤销。打开多个项目时，顶栏显示项目标签，回答中的引用和工具卡片带项目名并按项目区分颜色。“新对话”开始一个空白对话，之前的对话仍在历史中；刷新页面会自动回到上次打开的对话。服务在内存中最多保留 20 个会话，超出时移出最久未使用的会话，再次打开时从 `data/chat.db` 恢复。页面通过流式接口 `POST /api/sessions/{id}/messages/stream`（SSE，事件包括 `start`、`text_delta`、`tool_start`、`permission_request`、`permission_resolved`、`tool_end`、`project_opened`、`context_trimmed`、`done`、`stopped`、`error`）逐段显示回答；工具调用汇总为一行可展开的卡片，显示每次调用的目标、行数和失败原因；回答完成后显示本轮输入 Token、上下文占用和请求次数。回答过程中可点停止按钮或按 Esc 停止，前端调用 `POST /api/sessions/{id}/stop`，后端在收到下一段模型输出或执行下一个工具前结束，已停止的回答不计入对话历史；页面断开连接时同样会通知后端停止。同一会话上一条消息未完成时，新请求返回 409。对话相关接口：`GET /api/conversations`（历史对话）、`GET /api/sessions/{id}/messages`（恢复对话）、`PATCH /api/sessions/{id}`（重命名）、`DELETE /api/sessions/{id}`（永久删除）。项目相关接口：`GET /api/projects`（已授权项目）、`POST /api/projects/revoke`（撤销授权）、`GET`/`POST /api/sessions/{id}/projects`（当前会话的项目 / 加入已授权项目）、`POST /api/sessions/{id}/permissions/{request_id}`（对访问确认作出决定，请求已结束时返回 404）。两个服务仅供本机使用，按各自终端的 Ctrl+C 可停止。

## 项目结构

```text
niejiang-CodeChatAgent/
├── README.md
├── .gitignore
├── .env.example         # 模型配置示例
├── requirements.txt     # Python 依赖
├── backend/
│   ├── main.py          # 命令行对话入口
│   └── app/
│       ├── agents/       # 智能体及模型接入
│       ├── api/          # 后续聊天等接口
│       ├── services/     # 会话与项目等业务逻辑
│       └── tools/        # 目录浏览、搜索和文件读取工具
├── frontend/             # 后续聊天界面
└── tests/                # 项目测试
```

空目录中的 `.gitkeep` 文件用于让 Git 保留目录，不包含程序逻辑。目录职责会随实际开发调整。

## 试用对话

本机已创建独立的 Python 3.12 虚拟环境，并复用第七章的百炼配置到项目根目录的 `.env`。在本目录执行：

```bash
.venv/bin/python backend/main.py
```

出现提示后输入问题并按回车，可以继续追问；输入 `exit` 或 `quit` 退出，也可以按 Ctrl+D。退出后历史对话不会保留。

也可以直接传入问题，程序将只回答这一轮后退出：

```bash
.venv/bin/python backend/main.py '请解释 Python 函数的作用'
```

在 PyCharm 中运行 `backend/main.py` 时，建议将运行配置中的 Python 解释器设为本项目的 `.venv/bin/python`，以使用本项目已安装的依赖。运行后在控制台输入问题并按回车。

在其他环境首次使用时，先创建虚拟环境、安装依赖，再从示例创建 `.env` 并填写自己的模型配置：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

`.env` 包含密钥，已加入 Git 忽略规则。当前调用传入了百炼的 `enable_thinking: false` 参数，切换其他模型服务时可能需要调整。

`.env` 还需要两项上下文配置，缺失或无效时程序会拒绝启动：

- `LLM_MAX_INPUT_TOKENS`：模型最大输入长度，作为硬上限兜底；`qwen4-max`（北京区域、非思考模式）为 991808。
- `LLM_CONTEXT_BUDGET`：实际工作预算，当前取 128000。`qwen4-max` 北京区域输入 10 元、输出 30 元、缓存命中 0.5 元（每百万 Token，不分档）；工具循环每轮都会重发整个上下文，预算决定单次请求的最高输入成本（128K 约 1.28 元，命中缓存的部分按 5% 计价）。

`qwen4-max` 默认开启思考模式，本项目通过 `extra_body={"enable_thinking": False}` 关闭（见 `backend/app/services/chat_service.py` 的 `ChatOptions`）；关闭后无需回传 `reasoning_content`，工具调用的 `tool_choice` 使用 `auto` / `none`。

更换模型时，按百炼模型列表更新这两项。每轮回答结束时，命令行会显示本轮输入 Token 数和预算占用比例。

## 分析本地项目

直接启动即可聊天；需要分析项目时，在消息中写出项目的绝对路径（可用 `~/` 开头）：

```bash
.venv/bin/python backend/main.py
你：看看 ~/Projects/my-project/hello-agents 的入口在哪
```

也可以用 `--project` 预先打开一个或多个项目（可多次指定）：

```bash
.venv/bin/python backend/main.py --project ./backend --project ../another-project
```

回答会逐段打印，打开项目时显示“· 已打开项目 名称（技术栈，路径）”，工具调用以“· 工具名 参数 → ✓ 行数”的形式穿插显示。只打开一个项目时，引用格式如 `backend/main.py:19`；打开多个项目时在前面加项目简称，如 `my-agent:src/tools.ts:42`。

**项目授权**：助手只能打开以下两类项目，且只读：

- 用户在本轮消息中写出的路径（或其子目录），以及通过 `--project`、网页创建会话时指定的路径。打开后加入“已授权项目”清单。
- 已授权项目：之后在任何会话中都可以直接用简称提到它，如“看看 hello-agents”。

模型自行推断出的其他路径（例如代码中提到的另一个目录）需要你确认：网页中显示确认卡片（显示路径和剩余时间，可“允许访问”或“拒绝”），命令行中提示 `[y/N]`；120 秒内未确认视为拒绝，等待期间可以停止回答；允许后加入已授权项目，拒绝后本轮不再询问同一路径。系统目录、主目录本身和 `.ssh` 等凭据目录一律拒绝。已授权项目清单保存在本机的 `data/authorized_projects.json`（已加入 `.gitignore`），只记录简称、路径、概况和授权时间，不保存代码内容；删除其中的条目即撤销授权，正在使用该项目的会话会立即失去访问权限。每个会话最多同时打开 5 个项目，超出时移出最久未使用的项目（不影响授权）。

在 PyCharm 的运行配置中，可将 **参数（Parameters）** 设置为 `--project /你的项目绝对路径` 预先打开项目，也可以不设置。

代码工具共 5 个：`open_project`、`list_files`、`find_files`（按文件名或路径模式查找，如 `*Controller.java`、`src/**/*.ts`）、`search_code` 和 `read_file`。`search_code` 默认按字面文本、不区分大小写搜索，可用 `path` 限定子目录、`glob` 限定文件类型、`regex` 按正则匹配、`case_sensitive` 区分大小写、`context` 附带前后 0～5 行、`output` 选择返回匹配行（content）、只列文件（files）或只统计数量（count），`limit` 控制返回数量（默认 50，最多 200）；结果被截断时会说明总匹配数、文件数和未显示的文件，提示缩小范围。`read_file` 每次最多读取 500 行，并返回文件总行数。

文件读取同时支持 UTF-8 和 GBK/GB18030 编码。在 Git 仓库中搜索和查找文件时遵守 `.gitignore`（通过只读的 `git ls-files`，并关闭 fsmonitor 等可能执行外部命令的配置），不是 Git 仓库或目录本身被忽略时直接遍历目录。工具始终跳过 `.git`、虚拟环境、`node_modules`、构建目录、符号链接、`.env`、`.ssh`/`.aws` 等凭据目录和常见密钥文件（名称比较不区分大小写），搜索时还跳过依赖锁文件（如 `package-lock.json`）和压缩产物（`.min.js` 等）；单次搜索最多扫描 20,000 个文件、10 秒，单文件最大 1 MiB。每个问题最多调用 15 轮工具。搜索是文本匹配，不等同于语法级调用链分析。文件名排除规则不能识别所有敏感内容：**被读取或搜索到的代码片段会发送给配置的模型服务**，请只选择允许发送的项目。

## 后续步骤

1. 逐步实现调用链分析，并根据试用结果优化。

每完成一个阶段，先验证和试用，再进入下一阶段。

## 对话保存

网页对话保存在本机 `data/chat.db`（SQLite，`data/` 已加入 `.gitignore`）：

- 保存内容：对话标题、你的问题、助手回答的事件记录（文字、工具调用摘要、打开项目和访问确认的结果、用量信息）、用于恢复模型上下文的“问题 + 回答 + 工具摘要”，以及每个对话打开过的项目路径。
- 不保存：工具读取到的代码原文、密钥。没有任何输出就失败的提问不保存。
- 恢复：重新打开对话时恢复模型上下文（超过预算一半时与平时一样删除最早的轮次），并重新打开仍在已授权项目清单中的项目；已撤销授权的项目不会再打开。
- 删除：在侧栏删除对话会永久删除其全部记录；删除 `data/chat.db` 可清空全部对话。

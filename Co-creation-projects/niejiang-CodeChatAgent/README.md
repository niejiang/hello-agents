# CodeChatAgent

基于 Hello-Agents 的聊天式代码分析助手。计划通过对话理解项目结构、查找代码，并逐步分析调用关系。

## 当前进度

已接入 Hello-Agents 和百炼兼容接口，支持命令行和本机网页多轮对话，以及对指定本地项目的目录浏览、文本搜索和文件读取。会话只保留在服务进程的内存中，重启后不保存。

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

打开 <http://127.0.0.1:5173/>，输入本机项目的绝对路径并开始对话。切换项目会创建新会话并释放旧会话；刷新页面后需要重新创建会话。服务最多保留 20 个会话，超出时清理最久未使用的会话。页面目前一次性显示模型回复，工具调用过程仍在后端终端显示，流式事件和图表留待后续实现。两个服务仅供本机使用，按各自终端的 Ctrl+C 可停止。

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

## 分析本地项目

使用 `--project` 指定一个本地目录后进入多轮对话，例如分析本项目的后端：

```bash
.venv/bin/python backend/main.py --project ./backend
```

可以提问：“请查找 create_chat_agent 的定义，读取相关代码并解释它的作用。”控制台会显示调用了哪些工具；回答中的文件路径相对于选定的项目目录。

在 PyCharm 的运行配置中，将 **参数（Parameters）** 设置为 `--project /你的项目绝对路径`，然后运行 `backend/main.py`。不传 `--project` 时为普通聊天模式，无法访问本地文件。选择的目录在当前会话中固定；更换项目需重新启动程序。

工具默认跳过 `.git`、虚拟环境、`node_modules`、构建目录、符号链接、`.env`、`.ssh`/`.aws` 等凭据目录和常见密钥文件（名称比较不区分大小写）；每次搜索最多扫描 5000 个文件、返回 50 条结果，单次读取最多 200 行、单文件最大 1 MiB。搜索是文本匹配，不等同于语法级调用链分析。文件名排除规则不能识别所有敏感内容：**被读取或搜索到的代码片段会发送给配置的模型服务**，请只选择允许发送的项目。

## 后续步骤

1. 逐步实现调用链分析，并根据试用结果优化。

每完成一个阶段，先验证和试用，再进入下一阶段。

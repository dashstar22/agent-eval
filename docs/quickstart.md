# 本机运行

AgentEval 包含 FastAPI 后端、MySQL 评测库和 Vue 工作台。首次启动可以维护数据集、配置 Mock Agent，并体验运行、报告和 Badcase 页面，无需模型密钥。真实 Agent 调用与 LLM Judge 评分需要分别配置。

当前运行方式面向本机单用户，后端没有账号登录和多租户隔离。以下命令仅绑定回环地址；需要远程部署时，应另行配置认证、访问控制与 HTTPS。

## 环境

| 组件 | 要求 |
|---|---|
| Python | 3.12 或 3.13 |
| Python 环境管理 | [uv](https://docs.astral.sh/uv/) |
| Node.js | `^20.19.0` 或 `>=22.12.0`，附带 npm |
| 数据库 | MySQL 8.4，使用独立的 `agenteval_` 前缀 schema |
| 容器 | Docker，启用 Linux containers；也可自行准备独立 MySQL |

以下步骤使用 **Windows PowerShell**，从仓库根目录开始。后端运行依赖锁定于 `backend/uv.lock`，前端锁定于 `frontend/package-lock.json`。

## 1. 安装后端与创建数据库

```powershell
uv sync --project backend --locked
docker pull mysql:8.4.13
uv run --project backend --locked python -X utf8 scripts/group2_mysql.py
```

数据库脚本创建新的容器、随机密码、随机回环端口和独立 schema，不复用已有业务库。连接配置保存在被 Git 忽略的 `.local-validation/group2-mysql/settings.json`，命令只显示容器名、schema 和端口。

已有该配置时，脚本会拒绝重复创建；复用方法见下方“停止与再次启动”。如果自行准备 MySQL，请使用只对新建评测 schema 授权的用户，并在服务进程中设置以下格式的连接：

```text
mysql+pymysql://<user>:<URL编码后的密码>@127.0.0.1:<port>/agenteval_<schema名>?charset=utf8mb4
```

不要把真实连接字符串写入已跟踪文件。

## 2. 迁移、初始化并启动后端

在同一个终端执行：

```powershell
$mysqlConfig = Get-Content -Raw .local-validation/group2-mysql/settings.json | ConvertFrom-Json
$env:AGENTEVAL_DATABASE_URL = $mysqlConfig.url
cd backend
uv run --locked alembic upgrade head
uv run --locked python -m agenteval.seed
uv run --locked uvicorn agenteval.main:app --host 127.0.0.1 --port 18080 --workers 1
```

MySQL 首次启动需要数秒。如果迁移遇到连接未就绪，稍后重试 `alembic upgrade head` 即可。

初始化返回 `CREATED` 和 30 条启用用例；内容未改变时重复执行返回 `ALREADY_PRESENT`，不会覆盖已编辑的样例。数据是自编的打印机合成已见回归题，来源和 CC0 声明在 [`data/printer-demo`](../data/printer-demo/)。初始化不调用模型。

后端地址为 [http://127.0.0.1:18080](http://127.0.0.1:18080)，交互 API 文档为 [http://127.0.0.1:18080/docs](http://127.0.0.1:18080/docs)。应用启动不会自动迁移数据库。

后端同时只执行一个运行、重评分或重复运行批次，启动时持有该 schema 的 MySQL 独占锁。请保持 `--workers 1`，执行批次时不要开启 `--reload`。

## 3. 启动前端

新开终端，从仓库根目录执行：

```powershell
cd frontend
npm ci --ignore-scripts
npm run dev
```

访问 [http://127.0.0.1:15173](http://127.0.0.1:15173)。Vite 将 `/api` 代理到 `http://127.0.0.1:18080`。端口已被占用时会明确失败，不自动切换。

更换后端地址时，在启动前端的终端中设置：

```powershell
$env:AGENTEVAL_API_TARGET = 'http://127.0.0.1:18081'
npm run dev
```

## 4. 无密钥体验工作台

1. 在“数据集”查看已初始化的 30 条用例，展开问题、参考行为、预期工具和来源，也可以另建自己的测试集。
2. 在“Agent 配置”新建连接，适配器选择 `MOCK`，来源保留 `MOCK`，无需填写凭证。
3. 在“实验”选择数据集与该连接，创建实验并启动运行。Judge 配置保持空对象 `{}`，不设置真实模型连接。
4. 运行结束后进入报告，查看逐题详情、规则结果和 Badcase；刷新页面后记录仍保存在数据库中。

Mock 只返回带 `[MOCK]` 前缀的问题回显，不进行真实推理。未配置 Judge 时，质量指标会保留 `JUDGE_CONFIGURATION_UNRESOLVED` 等原因；用例可显示 `FAIL` 或 `UNDETERMINED`，不能将这次体验解释为真实质量评测。报告、详情和历史查询不会调用模型。

运行结束后可从侧栏进入“质量工作台”，完成结构化人工复核、基线回归检查、待审回归用例和稳定性分析。报告页可导出 JSON / CSV / HTML，数据集用例支持可选的工具顺序与次数断言。重复运行将创建新的 Agent / Judge 执行，真实来源需单独确认次数与合计预算。完整操作及 CLI 见[评测闭环扩展](evaluation-extensions.md)。

从旧版本升级时先停止后端并备份数据库，然后执行 `uv run --locked alembic upgrade head`。`20261117_0002` 新增可空工具断言字段与四张扩展表；不重算历史结果。普通启动不自动迁移。

## 5. 配置真实 Agent 与 Judge

两类连接可以使用不同服务和凭证：

| 连接 | 配置位置 | 用途 |
|---|---|---|
| 被测 Agent | “Agent 配置”中的 `HTTP` 或 `PRINTER_HTTP` | 生成回答、执行工具或业务动作 |
| Judge | “启动新运行”或“重新评分”中的 Judge JSON | 对已保存回答按量表评分 |

凭证字段填写**环境变量名称**。真实值须在启动后端的进程环境中提供；根目录 `.env.example` 是配置参考，应用不会自动加载该文件。更改后端环境后需要重新启动服务，使进程取得新值。

通用 `HTTP` 连接向配置地址 POST `execution_id`、`question` 和 `preconditions`，响应至少包含字符串 `answer`，可附工具、上下文、用量和实际版本信息。它不是直接连接任意聊天模型 API 的适配器；接入服务需要满足 [HTTP 协议](integration.md)。真实服务的来源选择 `REAL_EXTERNAL`。

Judge 使用 OpenAI 兼容的 Chat Completions 接口。例如，在后端进程已持有有效 `DASHSCOPE_API_KEY` 时，可以填写：

```json
{
  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
  "model": "qwen-plus",
  "credential_ref": "DASHSCOPE_API_KEY",
  "source_type": "REAL_EXTERNAL",
  "timeout_seconds": 60
}
```

模型和接口应以自己的供应商账号实际可用范围为准。新运行还需要显式填写总时限和请求上限。以下是一个 **30 条单轮用例的保护配置示例**，并非性能实测值或费用承诺：

```json
{
  "case_timeout_seconds": 360,
  "observation_wait_seconds": 10,
  "response_max_bytes": 2097152,
  "max_agent_calls": 1,
  "total_timeout_seconds": 7200,
  "max_total_calls": 90
}
```

`max_agent_calls` 限制每条用例的目标 Agent 轮次及显式前置轮次，`max_total_calls` 限制本次运行的平台 Agent/Judge 请求总量。Agent 内部可能另有多次模型、检索和工具请求，需要由源服务自行设置预算；平台上限不等于供应商 Token 数或人民币费用。建议先用少量用例验证配置，再运行整批。

`PRINTER_HTTP` 面向关联的 [打印机售后助手](https://github.com/dashstar22/printer-after-sales-assistant)。该应用不包含在本仓库中，需要另行部署支持认证、CSRF、咨询轮次与评测观测接口的兼容版本，并为用例配置当前账号有权访问的 `preconditions.order_id`。观测应在隔离演示实例显式启用，具体认证和观测契约见 [接入协议](integration.md)。本仓库的 30 条合成题不会自行创建售后账号、设备或知识索引。

启动真实运行和执行 RESCORE 都可能产生供应商用量。RESCORE 复用原回答，仅重新调用 Judge，保留旧评分；重新生成回答需要创建新运行。

## 停止与再次启动

前后端在各自终端按 `Ctrl+C` 停止。在仓库根目录停止本次数据库容器并保留数据：

```powershell
$mysqlConfig = Get-Content -Raw .local-validation/group2-mysql/settings.json | ConvertFrom-Json
docker stop $mysqlConfig.container
```

再次使用时，先启动同一容器并刷新可能变化的随机端口，再启动后端和前端：

```powershell
docker start $mysqlConfig.container
uv run --project backend --locked python -X utf8 scripts/group2_mysql.py --refresh-port
$mysqlConfig = Get-Content -Raw .local-validation/group2-mysql/settings.json | ConvertFrom-Json
$env:AGENTEVAL_DATABASE_URL = $mysqlConfig.url
cd backend
uv run --locked uvicorn agenteval.main:app --host 127.0.0.1 --port 18080 --workers 1
```

若新开了终端，先在仓库根目录重新读取 `$mysqlConfig`。启动后会识别没有执行者的旧活动任务并标记 `INTERRUPTED`，保留已经提交的回答和评分；不会自动重放 Agent 或续跑 Judge。

## 本地检查

```powershell
# 仓库根目录；使用测试夹具，不请求真实模型
uv run --project backend --locked pytest backend/tests

cd frontend
npm run typecheck
npm run build
```

普通后端测试使用 SQLite 事务和明确标记的 HTTP 协议替身，不能替代独立 MySQL 或真实模型验证。最近一次真实评测的配置、结果与限制见 [评测说明](evaluation.md)。

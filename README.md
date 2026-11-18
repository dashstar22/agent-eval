<div align="center">

<h1>AgentEval · AI Agent 评测工作台</h1>

<p><strong>用固定用例检查回答质量，用执行证据定位失败原因。</strong></p>

<p>从批量运行、规则与模型评分，到配置比较和失败分析的 Python / Vue 应用。</p>

<p>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.12%20%7C%203.13-3776AB?logo=python&amp;logoColor=white" alt="Python 3.12 or 3.13" /></a>
  <a href="https://fastapi.tiangolo.com/"><img src="https://img.shields.io/badge/Backend-FastAPI-009688?logo=fastapi&amp;logoColor=white" alt="FastAPI" /></a>
  <a href="https://vuejs.org/"><img src="https://img.shields.io/badge/Frontend-Vue_3-4FC08D?logo=vuedotjs&amp;logoColor=white" alt="Vue 3" /></a>
  <a href="https://www.mysql.com/"><img src="https://img.shields.io/badge/Database-MySQL-4479A1?logo=mysql&amp;logoColor=white" alt="MySQL" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-3DA66D" alt="MIT License" /></a>
</p>

<p>
  <a href="#验证摘要">验证摘要</a> ·
  <a href="#运行截图">运行截图</a> ·
  <a href="#功能">项目功能</a> ·
  <a href="#快速启动">快速启动</a> ·
  <a href="docs/architecture.md">架构说明</a> ·
  <a href="docs/evaluation.md">评测说明</a>
</p>

<p>作者：<a href="https://github.com/dashstar22">dashstar22</a></p>

</div>

---

## 项目介绍

AgentEval 是一个可在本机运行的 Agent 评测项目，将“准备测试集 → 批量执行 → 评分 → 比较配置 → 分析失败”连成完整流程。它适合在修改 Prompt、模型或工具流程后，检查原有能力是否退化，并追查具体用例的回答、工具结果和评分依据。

工作台结合程序规则和模型评分：程序规则检查执行状态、工具行为与时限，评分模型（LLM Judge）依据量表评价已保存的回答。失败用例分析（Badcase）用于整理原因和备注，再把需要持续检查的问题加入回归测试。

项目以 [纸序 · 打印机售后助手](https://github.com/dashstar22/printer-after-sales-assistant) 为接入案例，同时提供通用 HTTP 协议和本地模拟（Mock）适配器。评测工作台与被测应用独立运行，运行输入与各次评分单独保存。

这是一个使用 AI 辅助开发的个人学习与作品展示项目。仓库内的设备资料、问题和参考行为均为合成演示数据。

## 验证摘要

### 当前版本的工程验证

最近一次完整工程验证于 **2026-11-17** 完成，覆盖质量工作台及关联扩展：

| 检查 | 已保存结果 | 验证内容 |
|---|---|---|
| 后端测试 | **412 通过、1 跳过** | 业务逻辑、评分规则与扩展接口 |
| MySQL / HTTP / CLI | **29 项通过** | 独立数据库中的接口、持久化与命令行检查 |
| 浏览器交互 | **39 项通过** | 页面操作；四个质量页在 320px / 390px 下无整页横向溢出 |
| 前端类型、构建与依赖许可 | **通过** | 136 个包无未知许可；报告分块仍有约 523 kB 的体积提示 |

这轮使用合成测试记录与本地模拟的 Agent / Judge 接口，验证工程功能。跳过项是缺少外部原始资料包时的跨仓库校验。完整范围见[扩展验证回执](docs/evidence/evaluation-extensions-20261117.json)，各轮检查与截图核验见[验证历史](docs/validation-history.md)。

### 已保存的真实评测

**2026-11-17**，真实打印机 Agent 与 Judge 均使用 `qwen-plus`，对固定 30 条合成回归用例完成一轮评测：

| 核对项 | 结果 |
|---|---|
| 质量通过 / 失败 / 未判定 | **26 / 4 / 0** |
| 有效通过率 / 可判定覆盖率 | **86.67% / 100%** |
| Agent 执行成功 | **30/30** |
| Judge 成功评分 | **30/30** |
| 成功模型响应元数据覆盖 | **29/30** |

H08 在信息不足时按规则直接追问，没有请求模型，质量结论为通过。保留的四条失败为 **D11、H01、H02、H04**：D11 已检索到演示保修期仍未给出答案，其余三题遗漏知识答案、转为报修信息追问且未调用预期检索工具。

这份结果来自单次、已见合成回归集，Agent 与 Judge 同源，尚无独立人工盲审，不能据此推断通用准确率、模型排名或效果提升。实际版本证据仍不完整，供应商不可变模型快照未知，部分用量与费用也未取得。逐题结果、覆盖分母和用量见[真实端到端报告](docs/real-e2e-20261117.md)；评分方法与历史 A/B 解释见[评测说明](docs/evaluation.md)。

## 运行截图

下面展示评测报告、D11 失败详情和质量工作台。报告与 D11 详情来自上面的真实评测；质量工作台使用合成测试记录展示复核与一致性分析，不代表独立人工评审。**点击预览图或文字链接可查看完整页面。**

<p align="center">
  <a href="docs/images/real-e2e-report-full-20261118.png"><img src="docs/images/real-e2e-report-preview-20261118.jpg" width="960" alt="真实评测报告预览：26条通过、4条失败、0条未判定，点击查看完整页面" /></a>
  <br />
  <sub>评测报告 · 查看质量分布、用量与版本覆盖 · <a href="docs/images/real-e2e-report-full-20261118.png">查看完整报告</a></sub>
</p>

<p align="center">
  <a href="docs/images/real-e2e-detail-full-20261118.png"><img src="docs/images/real-e2e-detail-focus-preview-20261118.jpg" width="960" alt="D11重点预览：原问题询问演示保修期，参考行为明确为购买日起180天，实际回答却称依据未能确认" /></a>
  <br />
  <sub>D11 失败详情 · 对照原问题、参考行为与实际回答 · <a href="docs/images/real-e2e-detail-full-20261118.png">查看完整详情</a></sub>
</p>

<p align="center">
  <a href="docs/images/quality-workbench-full-20261118.png"><img src="docs/images/quality-workbench-preview-20261118.jpg" width="960" alt="质量工作台预览：人工复核表单与一致性分析，展示内容为合成测试记录" /></a>
  <br />
  <sub>质量工作台 · 整理人工复核并对照模型评分 · <a href="docs/images/quality-workbench-full-20261118.png">查看完整工作台</a></sub>
</p>

## 功能

当前版本包含 **Dashboard、数据集、Agent 配置、实验、评测详情、Badcase、报告、质量工作台**八个页面：

| 能力 | 可以做什么 | 页面入口 |
|---|---|---|
| 运行概览 | 按来源查看最近一次运行的质量概览、近期运行与对应报告 | Dashboard |
| 数据集与用例 | 编辑问题、参考行为、必评项、预期工具、前置轮次、标签和来源许可，按需启停；运行时保存输入快照，之后编辑不改变历史 | 数据集 |
| Agent 接入 | 配置 Mock、通用 HTTP 或打印机售后连接，检查凭证环境变量是否可用，记录预期版本 | Agent 配置 |
| 后台批量运行 | 选择数据集和 Agent，设置评分模型、规则、时限与调用上限，查看执行和评分进度 | 实验 |
| 规则与模型评分 | 检查执行、工具与时限；评价回答正确性、相关性、任务完成度及适用时的依据充分性，保留评分理由和尝试记录 | 评测详情 |
| 失败证据追查 | 按通过、失败、未判定或原因筛选，对照原问题、参考行为、实际回答、工具轨迹与上下文 | 报告 → 评测详情 |
| 配置与版本比较 | 核对数据与评分口径，在共同有效用例上比较指标并查看双方回答；版本证据不足时显示未验证 | 实验 / 报告 |
| 独立重新评分 | 对已保存回答重新评分，分别查看原评分与新评分；保留历史结果，无需再次执行 Agent | 实验 / 评测详情 |
| 失败用例整理 | 按分类或人工原因筛选，保存标签与备注；不同评分集的记录分别保留 | Badcase / 评测详情 |
| 统计、图表与用量 | 分别统计通过、失败、未判定和工具结果，从图表进入对应的用例；分别记录 Agent 与 Judge 用量，未知用量和费用留空 | 报告 |
| 结构化人工复核 | 先隐藏模型评分进行复核，保存人工结论、可选指标分与历史记录，再分析人工和模型评分的一致性 | 质量工作台 → 人工复核 |
| 基线回归检查 | 比较已保存的基线与候选结果，检查可比性、覆盖、新增失败和通过率下降；支持命令行调用 | 质量工作台 → 回归检查 |
| 工具行为断言 | 检查工具首次出现顺序和调用次数上限，保留实际事件证据 | 数据集 / 评测详情 |
| 失败用例回流 | 整理新的参考行为，审核后生成默认停用的回归用例，保留来源链接，再按需启用 | 质量工作台 → 回归候选 |
| 离线报告 | 下载当前评分集的 JSON、CSV 或自包含 HTML，保留脱敏证据与单独的人工复核历史 | 报告 |
| 重复执行与稳定性 | 沿用来源运行的原始输入和配置，新增 2–5 次独立执行；比较 2–10 次运行的结论变化与指标波动 | 质量工作台 → 稳定性 |

典型流程：**准备数据集 → 配置 Agent → 创建实验并运行 → 查看报告与失败详情 → 整理原因 → 修改配置后创建新运行，或对已保存回答重新评分**。同一次执行可以保留多个评分集，分别查看和比较。

重评分会调用 Judge；重复执行会重新调用 Agent 与 Judge。只查看、导出或分析已保存结果不会调用模型。工具匹配口径见[评分规则](docs/evaluation.md#程序规则)，人工复核、回归检查及重复运行的操作见[质量工作台与关联扩展](docs/evaluation-extensions.md)。

## 技术栈与结构

后端使用 Python 3.12 / 3.13、FastAPI、SQLAlchemy、Alembic、MySQL、HTTPX 和 Pydantic；前端使用 Vue 3、TypeScript、Vite、Element Plus 和 ECharts。

```text
Vue 工作台
    │ HTTP API
FastAPI 应用 ───────────────────────────── MySQL
    ├─ 数据集 / 配置 / 实验 ── 保存运行输入与版本摘要
    ├─ 后台执行器
    │    └─ Agent 适配器 → 保存回答与观测 → 规则 / Judge
    └─ 评分集 / 配置比较 / 失败分析 / 报告 / 质量工作台
```

后台通过数据库锁和唯一活动任务约束，按**单进程、单活动批次**运行。进程中断后会标记状态，后续由使用者决定是否创建新运行。模块职责与设计取舍见[架构说明](docs/architecture.md)。

## 快速启动

准备 **Python 3.12 或 3.13、[uv](https://docs.astral.sh/uv/)、Node.js 20.19+（20.x）或 22.12+**。数据库使用独立 MySQL 8.4；以下首次启动示例使用 **Windows PowerShell 和启用 Linux containers 的 Docker**，也可按[运行手册](docs/quickstart.md)自行配置 MySQL。

克隆仓库、安装后端依赖并创建独立演示数据库：

```powershell
git clone https://github.com/dashstar22/agent-eval.git
cd agent-eval
uv sync --project backend --locked
docker pull mysql:8.4.13
uv run --project backend --locked python -X utf8 scripts/group2_mysql.py
```

脚本创建新的容器、随机密码与独立数据库，连接配置保存在被 Git 忽略的本地文件中。MySQL 首次启动需要数秒；如果下一步迁移遇到连接未就绪，稍后重试迁移命令即可。已有配置时的再次启动方法见[运行手册](docs/quickstart.md)。

在同一个终端读取连接配置、初始化并启动后端：

```powershell
$mysqlConfig = Get-Content -Raw .local-validation/group2-mysql/settings.json | ConvertFrom-Json
$env:AGENTEVAL_DATABASE_URL = $mysqlConfig.url
cd backend
uv run --locked alembic upgrade head
uv run --locked python -m agenteval.seed
uv run --locked uvicorn agenteval.main:app --host 127.0.0.1 --port 18080 --workers 1
```

新开终端，从仓库根目录启动前端：

```powershell
cd frontend
npm ci --ignore-scripts
npm run dev
```

打开[本机工作台](http://127.0.0.1:15173)；后端交互式 API 文档为 [Swagger UI](http://127.0.0.1:18080/docs)。初始化导入 30 条合成用例，新数据库的历史报告为空；README 截图展示的是已保存的评测和演示记录。

**无需模型密钥**即可维护数据与配置，并用 Mock Agent 体验执行链路。Mock 只返回本地模拟回答；未配置有效 Judge 时，相关质量项会显示评分错误或未判定。首次体验步骤见[运行手册](docs/quickstart.md#4-无密钥体验工作台)。

**真实评测**需要自行部署兼容的被测 HTTP 服务、配置 Agent 与 Judge 凭证，并会产生模型用量。打印机专用接入还依赖被测应用的认证、设备归属与观测接口，详见[接入协议](docs/integration.md)。工作台面向本机单用户使用，后端未内置账号登录与多租户隔离。已有数据库升级前应先备份，再执行迁移。

需要检查代码时，新开终端并从仓库根目录执行：

```powershell
cd backend
uv run --locked pytest
cd ../frontend
npm run build
```

常规测试使用本地数据库或模拟接口，不请求外部模型。MySQL 专项检查需要单独配置独立测试库；各轮结果与适用条件见[验证历史](docs/validation-history.md)。

## 目录与文档

```text
backend/       FastAPI 应用、数据库迁移与测试
frontend/      Vue 工作台与前端依赖锁
data/          合成用例、Judge 校准样例与来源许可
scripts/       独立数据库、Judge 校准和结果复算工具
docs/          运行、架构、接入协议、评测记录与页面截图
```

从[运行手册](docs/quickstart.md)开始体验项目；接入自己的 Agent 时查看[协议说明](docs/integration.md)；理解指标和结果时查看[评测说明](docs/evaluation.md)；需要核对历史验收与证据时查看[验证历史](docs/validation-history.md)。

## 联系与许可

欢迎通过 [Issues](https://github.com/dashstar22/agent-eval/issues) 反馈复现问题，或在 [GitHub](https://github.com/dashstar22) 交流 AI 应用与评测实践。

自有源码按 [MIT License](LICENSE) 开源。合成题目和演示资料使用各目录内的 CC0-1.0 声明；第三方依赖保留各自许可，见[第三方来源与许可](docs/third-party-notices.md)。演示资料不构成真实设备维修指导。

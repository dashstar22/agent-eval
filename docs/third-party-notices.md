# 第三方来源与许可

自有源码按根目录 [MIT License](../LICENSE) 发布。第三方包与合成数据使用各自声明的许可；项目的 MIT 许可不替代这些许可。以下记录核对于 2026-11-16。

## 运行与构建依赖

依赖精确版本见 [Python 项目配置](../backend/pyproject.toml)、[uv.lock](../backend/uv.lock)、[前端项目配置](../frontend/package.json) 与 [package-lock.json](../frontend/package-lock.json)。已安装分发包的许可和来源元数据分别记录在 [Python 许可清单](../backend/dependency-licenses.json) 与 [前端许可清单](../frontend/dependency-licenses.json)。

| 直接依赖 | 许可 | 用途 |
|---|---|---|
| FastAPI、SQLAlchemy、Alembic、PyMySQL、Pydantic | MIT | API、数据校验、持久化和迁移 |
| HTTPX、Uvicorn | BSD-3-Clause | 有界 HTTP 调用与应用服务 |
| cryptography | Apache-2.0 OR BSD-3-Clause | MySQL 缓存 SHA2 认证支持 |
| Vue、Vue Router、Axios、Element Plus | MIT | 前端组件、路由与请求 |
| ECharts | Apache-2.0 | 报告图表 |
| Vite、@vitejs/plugin-vue、vue-tsc | MIT | 前端开发、构建与类型检查 |
| TypeScript | Apache-2.0 | 前端类型系统 |
| pytest、Hatchling | MIT | 后端测试与打包 |

清单包括运行、开发、传递和可选平台依赖。包元数据核查不等于逐文件源码审计；Hatchling 临时构建环境的传递工具也不属于 uv 运行锁文件的锁定范围。MySQL、Docker、Node.js 等由使用者自行安装，遵循各自许可。

前端许可清单可在安装锁定依赖后重新生成：

```powershell
cd frontend
npm ci --ignore-scripts
npm run licenses
```

## 设计参考

以下项目用于比较设计和理解公开问题，没有作为 AgentEval 的运行依赖安装，也没有直接复制其业务源码。

| 项目与核对版本 | 许可 | 参考范围 |
|---|---|---|
| [Opik](https://github.com/comet-ml/opik/tree/5ea60d2633a022f0ddb31d2ec81ef21810e0fe52) | Apache-2.0 | 数据集、实验、独立执行身份与结果关联 |
| [DeepEval](https://github.com/confident-ai/deepeval/tree/c0bd9c89f33853b2dedc7db8415b3036894fb204) | Apache-2.0 | 指标输入依赖、结构化 Judge 理由与错误处理 |
| [AgentEvals](https://github.com/langchain-ai/agentevals/tree/946ad15b9f2ce9bc9843edd2bb773bd07d78ca7e) | MIT | 工具轨迹、名称集合与匹配语义 |
| [FastAPI](https://github.com/fastapi/fastapi) | MIT | 应用生命周期与数据库会话管理 |
| [APScheduler](https://github.com/agronholm/apscheduler) | MIT | 后台任务身份与重复执行问题的比较；未安装该框架 |
| [create-vue](https://github.com/vuejs/create-vue) | 核心 MIT、模板 CC0-1.0 | Vue 工程组织；未复制模板代码 |

调研时参考了 [Opik #9566](https://github.com/comet-ml/opik/issues/9566) 的非拉丁文本指标问题及 [AgentEvals #114](https://github.com/langchain-ai/agentevals/issues/114) 的参数匹配问题。本项目没有将英文文本指标直接解释为中文回答正确性，也不宣称已经实现工具参数或调用顺序评分。量表为项目自定义的 1—5 分制，不代表上述框架的指标或行业基准。

## 合成数据与截图

- [30 题数据集](../data/printer-demo/dataset.json)、扩展题与自编资料的授权见 [数据许可](../data/printer-demo/LICENSE.txt)。
- 来源副本保留原有 [CC0 声明](../data/printer-demo/sources/LICENSE.txt)，原始文件身份和 SHA-256 记录在 [来源清单](../data/printer-demo/source-manifest.json)。这些资料来自同作者的 [打印机售后项目](https://github.com/dashstar22/printer-after-sales-assistant)，不是厂商手册或真实客户资料。
- [Judge 校准样例](../data/judge-calibration/cases.json) 按 [该目录许可](../data/judge-calibration/LICENSE.txt) 提供。
- `docs/images/` 是本项目实际运行截图，题目和业务内容为合成数据；其中显示的模型回答与评分仅用于记录对应运行。

供应商名称、模型名称、商标和使用者另行导入的资料不因项目公开而获得新的授权。真实模型调用由使用者依据供应商条款与价格自行配置。

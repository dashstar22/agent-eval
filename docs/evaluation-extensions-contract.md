# 评测闭环扩展：实施契约

本轮承接用户“都一起做，一步到位”，一次交付六项功能：结构化人工复核、基线回归检查、工具顺序与次数断言、Badcase 转待审回归用例、报告导出、重复运行与稳定性分析。沿用 Vue / FastAPI / MySQL 和既有视觉组件；不增加 Redis、MQ 或外部服务依赖。旧运行、评分、人工备注及证据保持原样。本文记录约定，不代表尚未完成的验证结果。

## 实施分工

- 主控：`schemas.py`、`services.py`、`persistence.py` 的工具断言字段；`rules.py`；新增迁移；`repeat_*.py`；`runtime_control.py` 的重复批次接入；`main.py` 路由注册；集成验证与交付文档。
- 人工复核 / 用例闭环：新增 `review_models.py`、`review_schemas.py`、`reviews.py`、`review_api.py` 和独立测试；不编辑共享 ORM、迁移或 main。
- 回归 / 稳定性 / 导出：新增 `analysis_schemas.py`、`analysis.py`、`analysis_api.py`、`regression_cli.py` 和独立测试；不编辑共享比较算法或既有 API。
- 前端：新增 `/quality` 质量工作台及独立组件、扩展 API / 类型；路由、详情 / 报告入口、数据集的工具断言编辑。沿用现有组件；不重做原有页面。

## 共同 API 边界

新接口统一 `/api/v1/extensions` 前缀，由 `add_review_routes(router, get_session)`、`add_analysis_routes(router, get_session)`、`add_repeat_routes(router, get_session)` 注册。错误沿用 `ApiError` 和严格输入模型。UUID 使用现有规范校验。读取与分析绝不触发模型调用。

### 人工复核

- `GET /extensions/reviews/cases?run_id=...&score_set_id=...`：返回专用盲审用例列表，不加载 Judge 结论。
- `GET /extensions/reviews/cases/{case_run_id}?score_set_id=...&blind=true`：返回冻结问题、参考行为、回答、上下文及适用指标；blind 默认 true，`judge:null`，不返回 Judge 分数、理由或整体结论。人工复核独立于原人工备注。
- `POST /extensions/reviews`：`{case_run_id, score_set_id, reviewer_name, conclusion: PASS|FAIL|UNDETERMINED, scores: {correctness?:1..5,relevance?:1..5,task_completion?:1..5,faithfulness?:1..5}, reason, human_confirmed:true}`。追加不可覆盖的复核记录；不修改 Judge。所有分数严格整数；记录本机填报身份未经认证，不宣称第三方独立质量证明。
- `GET /extensions/reviews?run_id=...&score_set_id=...`：返回复核历史及最新有效复核列表。`GET /extensions/reviews/consistency?...`：每题采用最新一次人工复核，统计与已保存评测结论的一致数/有效数；Judge 指标一致性另列有效配对数、精确一致数、平均绝对分差及分歧题。无有效配对返回 null，不把漏评当 0。

### Badcase 转回归用例

- `POST /extensions/regression-drafts`：`{case_run_id, score_set_id, target_dataset_id, case_key, expected_answer, reason}`，保存待审核候选，问题/工具预期/前置条件从原冻结用例复制，保留来源 run/case/score 与人工理由，不复制失败回答为标准答案。
- `GET /extensions/regression-drafts?target_dataset_id=...`：列出候选（可选 dataset，返回 items）。
- `POST /extensions/regression-drafts/{id}/approve`：`{reviewer_name, human_confirmed:true}`，在短事务中创建**禁用**用例，标记来源和 seen_regression=true；显式审核后再从数据集启用。重复审核幂等，同名冲突返回 409，不覆盖现有用例或历史。

### 回归检查

- `POST /extensions/regression-checks`：`{baseline:{run_id,score_set_id?},candidate:{run_id,score_set_id?},policy:{max_new_failures:0,min_coverage:1.0,max_pass_rate_drop:0.0}}`，policy 字段可省略取默认。返回 `verdict: PASS|FAIL|INCONCLUSIVE`、conditions、checks、new_failures、fixed_cases、unknown_cases、comparison。完成状态、可比性、有效分母/覆盖和版本证据分别说明；不可比/运行未完成/覆盖不足不能 PASS。
- CLI：`python -m agenteval.regression_cli --base-url ... --baseline ... --candidate ... [--baseline-score ... --candidate-score ...] [--format json|markdown] [--output ...]`。仅分析保存结果；退出码 0 通过、1 不通过、2 无法判断、3 请求或输入错误。

### 稳定性分析与重复运行

- `POST /extensions/stability-analysis`：`{selections:[{run_id,score_set_id?}, ...]}`，2..10 个不同实际运行，不能把同一执行的多个 RESCORE 当重复运行。核对冻结用例、Agent、Judge、规则和预算，条件一致才统计；返回 comparable、limitations、run_count、runs、cases、metrics。每题显示 P/F/U、已知结论翻转、有效观测数；指标给有效样本、均值、标准差及范围，未知不补零。实际版本核验与配置一致分开展示。
- `POST /extensions/repeat-batches`：`{request_id,source_run_id,repeat_count:2..5,confirm_real_execution:false}`，按来源运行的**冻结输入**创建独立新运行和 ORIGINAL 评分集，顺序执行，共用既有单活动槽。仅源运行 COMPLETED 可用。真实 Agent 或 Judge 必须显式确认，沿用且要求明确每轮预算；返回合计最大调用/时长估计，禁止自动扩大预算。
- `GET /extensions/repeat-batches/{id}` 返回 id/status/source_run_id/repeat_count/runs（id/status/score_set_id）/budget/error；`GET /extensions/repeat-batches` 返回 items。进程中断保留已执行记录，未执行成员标记中断，不能自动重放或续跑。批次请求号幂等，同号不同参数返回 409。

### 报告导出

- `GET /extensions/reports/{run_id}/export?score_set_id=...&format=json|csv|html`。返回下载附件；JSON 保留结果、冻结配置、来源、版本、有效分母和脱敏证据；CSV 每题一行并处理公式注入；HTML 可离线打开，所有用户文本转义，无外部脚本。导出固定评分集，不触发 Agent/Judge；不将历史证据或协议替身改称真实质量结果。

### 工具行为断言

CaseCreate/CasePatch 新增 `tool_assertions: {sequence?: string[], max_calls?:{[name]:非负整数}}`，默认空，不改变旧用例必评项或冻结历史。新增可选指标 `tool_order`、`tool_call_count`；仅配置后生成，可显式加入 required_metrics。顺序定义为所列工具**首次出现顺序**，每个名称必需出现；允许其他工具与后续重复调用。次数针对 profile.tool_sources 范围内的 TOOL 事件。轨迹未知、不完整或有未知来源时为 NA；证据包含事件 ID/序号、实际次数和配置。配置空序列、重复名称、空名称、负次数或无对应配置的必评指标应拒绝。

## 验收要求

自动测试覆盖边界、历史保护和真实持久化语义；测试用 Mock / PROTOCOL_DOUBLE，不发真实模型请求。MySQL 迁移和页面验证采用新隔离 schema。人工复核 API 的 human_confirmed 代表用户声明，不等于完成任何真实人工质量标注。浏览器必须覆盖六项实际入口与下载、错误/空态和窄屏；构建、许可证检查和原有测试回归通过后才记录为完成。

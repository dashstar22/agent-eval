# 评测扩展的开源调研与分析模块接口

2026-11-17 通过 GitHub 官方 API、仓库 README、许可证和仓库内文档只读核实。只借鉴设计思路，未安装依赖、复制源码或请求模型。

## 项目与许可证

| 项目 | 维护迹象 | 许可证与边界 | 借鉴点 |
| --- | --- | --- | --- |
| [promptfoo](https://github.com/promptfoo/promptfoo) | 未归档；最近 push 2026-11-17T08:31:35Z | [MIT](https://github.com/promptfoo/promptfoo/blob/main/LICENSE) | 人工 Pass/Fail/评分/评论持久化、可导出；阈值与退出码作为回归门禁 |
| [DeepEval](https://github.com/confident-ai/deepeval) | 未归档；最近 push 2026-11-16T00:36:11Z | [Apache-2.0](https://github.com/confident-ai/deepeval/blob/main/LICENSE.md) | 工具名称、顺序、次数及结果的确定性检查；阈值和失败原因一起保留 |
| [Langfuse](https://github.com/langfuse/langfuse) | 未归档；最近 push 2026-11-17T11:57:04Z | [核心 MIT，指定 ee 路径商业许可](https://github.com/langfuse/langfuse/blob/main/LICENSE)；不能将整个仓库称作 MIT | 人工标注草稿与查询数据分离、失败重试、迟到响应不改变新选择 |

维护时间只是当前活跃迹象，不构成无缺陷或质量保证。

## 来源与取舍

- [promptfoo Web UI](https://github.com/promptfoo/promptfoo/blob/main/site/docs/usage/web-ui.md#cell-actions)：评级与评论持久化并导出。AgentEval 保留结构化人工复核与原 Judge 独立记录，不直接覆盖自动结论。
- [promptfoo CI/CD](https://github.com/promptfoo/promptfoo/blob/main/site/docs/integrations/ci-cd.md#setting-quality-gates)：检查阈值与进程退出码。AgentEval 仅分析保存结果，并增加 INCONCLUSIVE 处理未知、口径不同和覆盖不足。
- [DeepEval 工具正确性](https://github.com/confident-ai/deepeval/blob/main/docs/content/docs/%28agentic%29/metrics-tool-correctness.mdx)：基础匹配可确定性执行；提供 available_tools 才增加模型最优性判断。本项目只做声明式确定性断言，不引入模型判工具最优。
- [DeepEval CI](https://github.com/confident-ai/deepeval/blob/main/docs/content/docs/evaluation-unit-testing-in-ci-cd.mdx)：收集结果和阻断构建具有不同语义。门禁使用明确退出码，不把生成了报告当作通过。
- [Langfuse 标注状态设计](https://github.com/langfuse/langfuse/blob/main/web/src/features/annotation-queues/README.md)：保护编辑草稿、串行状态转移和处理迟到响应。这里简化成 run/score/case 上下文，不添加多人锁调度服务。
- [promptfoo 自定义断言限制](https://github.com/promptfoo/promptfoo/blob/main/site/docs/configuration/expected-outputs/javascript.md#restrict-inline-assertions-and-transforms)：safe mode 不是脚本沙箱。本项目不接受任意 Python/JavaScript 表达式。

## 分析 DTO 附录

正式请求路径与字段以 [实施契约](evaluation-extensions-contract.md) 为准；下述字段用于前后端冻结。

### 回归检查响应

`verdict: PASS|FAIL|INCONCLUSIVE`；`policy` 为实际使用阈值；`scope=SAVED_RESULTS_ONLY_NO_NEW_AGENT_OR_JUDGE_CALLS`。

- `conditions {completed, comparable, coverage_sufficient, limitations:string[], version_evidence:{baseline,candidate,comparison}, source_scope:{baseline,candidate}}`。
- `checks[] {id,label,status:PASS|FAIL|INCONCLUSIVE,actual:number|null,threshold:number|null,reason:string|null}`。
- `new_failures[]`、`fixed_cases[]`、`unknown_cases[]` 为既有 Comparison.cases 行，并附 `baseline_link`、`candidate_link`。
- `comparison` 为既有完整比较 DTO，不修改其均值或分母。

新增失败仅指已知 PASS→FAIL；修复为已知 FAIL→PASS。无结论、用例不匹配或仅一侧有效进入 unknown。通过率下降取共同有效结论的配对差值；覆盖门槛同时核对两侧可判定覆盖与共同配对覆盖。运行/评分未完成、不可比、无共同有效题或覆盖不足不能 PASS。版本与真实来源作为独立证据展示，保存结果门禁通过不代表完整真实质量或统计显著。

### 稳定性响应

- `comparable`、`limitations:string[]`、`run_count`。
- `runs[] {run_id,score_set_id,run_status,score_set_status,kind,summary,source_scope,version_verification}`。
- `cases[] {case_key,P,F,U,valid_count,total_count,flipped:boolean|null,flip_count:number|null,observations:[{run_id,score_set_id,case_run_id,conclusion,agent_status}],links:string[]}`。
- `metrics[] {metric,valid_count,total_count,mean,stddev,min,max,reason,unavailable_reasons,aggregation,stddev_kind}`。
- `conditions {frozen_conditions_match,completed,actual_versions_verified}`。

2..10 个 distinct run；同一 run 的不同 RESCORE 不算重复执行。逐项核对 dataset、完整冻结 cases、Agent、所选 Judge/规则、预算和组件版本，排除运行身份/时间/实验身份等管理字段。条件不一致或运行未完成时不输出合并均值/标准差/范围及翻转结论；仍保留各运行事实与有效数。指标统计取有效逐题执行观测的总体均值和总体标准差；单个有效值标准差 null。请求运行顺序决定翻转顺序，跳过未知且明确保留 U。

### 离线导出

JSON 包含 `schema_version,scope,report,cases,human_reviews,limitations`，逐题 detail 包含该评分集的评分、Judge 尝试、工具轨迹、观测、用量、版本和人工备注，以及 `human_reviews` 历史与 `latest_human_review`。人工结论单列，不替代 Judge，只导出所选评分集的复核历史。CSV 每题一行、复杂字段 JSON 编码，处理公式注入。HTML 无外部资源或脚本，全部用户文本转义，包含完整明细与填报身份说明。文件名同时包含 run 与实际评分集 ID。导出不增加执行、评分或模型用量。

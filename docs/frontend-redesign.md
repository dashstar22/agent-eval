# 前端改版实施与验证

本次按 2026-11-17 用户确认的方案调整现有 Vue 工作台，优先报告和评测详情。原 `build-agenteval-platform` 为 58/58、all_done；本次不重写其验收结论，不新增后端能力、不重新执行 Agent 或 Judge。

## 已完成的改版

- 视觉基础：统一中文界面字体与数字字体、12/14/16/18/28/36px 字号层级、4px 间距体系、蓝灰背景与边框、蓝色操作、红色失败、琥珀色未判定；通过 CSS 变量同步 Element Plus 控件。按钮、筛选器、状态标签、结果面板和证据提示复用同一组基础组件。
- 报告页：运行和评分集集中在顶部；质量 P/F/U、有效通过率和可判定覆盖率前置；突出“查看失败用例”，使用报告实际返回的评分集跳转。Badcase 分类和指标位于结果下方；证据范围始终可见，完整版本核验、来源、用量、冻结配置在证据区展开。
- 对比报告：可按需添加对照，已有对比深链自动展开；保留四类图表、实际左右运行及成员钻取。图表改为横向条形并显示数值，说明配对范围、有效样本数和缺失原因。
- 评测详情：宽屏使用列表与详情分栏，窄屏提供详情与返回列表；前置失败指标和已保存理由，区分执行、质量、评分三类状态。保留问题、参考行为、实际回答、工具轨迹、检索证据、用量、Judge 尝试、版本及原始记录。
- 筛选与人工标记：增加通过/失败/未判定快捷筛选；保留原因多选、Badcase 人工原因、评分异常及分页。人工标记按用例和评分集隔离，保存成功不会因列表刷新丢失提示；切换记录、评分集或卸载后忽略旧请求的迟到响应。

## 共享契约

- 主控：`tokens.css`、`style.css`、基础组件 `PageHeading`、`StatusBadge`、`EvidenceNotice`、`SectionCard`、`ResultSummary`、现有 `RunPicker`、全局注册和最终集成验证。
- 报告：`ReportsView.vue`、`SummaryPanel.vue`、`ReportChart.vue`，其页面专用样式保留在组件内。
- 详情：`EvaluationsView.vue`、`CaseDetailPanel.vue`，其页面专用样式保留在组件内。
- API、后端数据模型、计算、历史证据及原 OpenSpec 清单保持原有合同。前端类型仅补充消费后端已存在的可选图表字段 `left_valid_count`、`right_valid_count`、`mean_scope`、`reason`，没有新增接口。

基础组件 Props：

- `PageHeading { title, description?, eyebrow? }`，`actions` 插槽用于操作，默认插槽用于上下文。
- `StatusBadge { value?, domain?: quality|execution|scoring|run|verification|neutral }`，默认 neutral；COMPLETED 不映射质量通过，FAILED 按所属轴解释。
- `EvidenceNotice { title, description?, tone?: info|warning }`，`actions` 插槽提供展开入口。
- `SectionCard { title?, description? }`，`heading` / `actions` 插槽可选。
- `ResultSummary { summary: Summary, runId?, scoreSetId? }`，质量 P/F/U 各自可进入 `/evaluations` 的对应结论；保持冻结总数和两项率的现有分母。
- `RunPicker` 保留现有 props/events，新增 `compact?` 支持上下文栏；根据用途区分“全部运行”与“不选择对照运行”。
- `SummaryPanel` 新增 `mode?: all|metrics|details`（默认 all）；Dashboard 和实验页继续使用默认展示。

## 必须保留的行为

运行/评分集选择；报告四类图表及准确左右钻取；来源、版本核验、冻结条件；所有指标的有效数与分母；加载/空态/失败重试；分类多选、分页、Badcase 人工原因与评分异常；单题全部评分理由、工具轨迹、证据、Judge 尝试及评分集隔离的人工标记。

质量失败 F、未判定 U 与评分故障分别呈现；自动原因可重叠；不可比和空配对不伪造差值；未知观测不当作零或空调用。

Badcase 分类可能包含可选指标失败但整体质量为 PASS 的用例，不能等同 F；图表保持中性色并注明范围。`COMPLETED` 表示处理完成；`PROTOCOL_ERROR` 表示协议错误；`RESULT_UNKNOWN` 表示执行结果未知。指标单位分别展示，不把所有规则分数标为 /5。详情上一条/下一条仅遍历本页，跨页仍从列表翻页。

## 验证记录

验证日期：2026-11-17。本次没有新增依赖、没有修改锁文件、没有提交或推送代码。

| 检查 | 结果与范围 |
| --- | --- |
| `npm run build` | PASS，包含 `vue-tsc --noEmit`；Vite 仍有报告分块超过 500kB 的体积提示，约 522.63kB、gzip 176.88kB，不影响构建成功。 |
| `npm run licenses` | PASS，扫描 136 个包，unknown 为空。 |
| `git diff --check` | PASS。 |
| 浏览器交互 | PASS，41 项有效断言；覆盖失败/未判定入口、四类图表、左右成员钻取、证据展开、分页、多选、Badcase 筛选、键盘激活、加载故障/重试，以及人工标记保存、重载持久化、评分集隔离与迟到响应。Dashboard、实验页的共享组件也已回归。 |
| 窄屏几何检查 | PASS，按实际 `innerWidth` 验证 320px/390px 的报告与详情无整页横向溢出，并检查窄屏返回与筛选保留。桌面截图已人工目视检查；窄屏几何/交互通过不代表全尺寸像素验收。 |
| 隔离 MySQL / HTTP | PASS，17 项断言；真实注释 PUT/GET、重载持久化、评分集隔离、503 不提交、延迟请求身份、运行时禁用及历史文件不变。 |
| 历史结果保护 | PASS，8 项保存结果 API 响应和 76 个原有证据/OpenSpec 文件的 SHA-256 与改版前基线一致。历史 A 仍为 N=30、P=25、F=5、U=0，有效通过率 83.3%、可判定覆盖率 100%。 |

验证数据分两部分：历史 G7 已保存结果只读回放；另建隔离 schema 保存 24 条 `PROTOCOL_DOUBLE` 合成固定事实及两个评分集，用于可写的人工标记、分页、错误和请求竞态验证。验证期间禁用运行时，Agent 请求和 Judge 请求均为 **0**。上述检查证明界面行为与既有口径保留，不构成新的真实模型质量评测，也不改变历史 G7 的证据范围或版本核验结论。

可复核交付：

- [结构化验收记录](evidence/frontend-redesign-20261117.json)
- [报告页截图](images/frontend-report-20261117.png)
- [详情页失败理由截图](images/frontend-detail-20261117.png)

截图来自历史保存结果，详情截图为滚动到失败理由后的视图。仅裁掉浏览器截图右侧和底部空白，页面内容未改动。私有原始回执保存在 Git 忽略的 `.local-validation/frontend-redesign/`；两条早期被误命名为 390px、实际约 488px 的重复断言未计入 41 项，最终宽度结论以明确核验 `innerWidth` 的 320px/390px 断言为准。

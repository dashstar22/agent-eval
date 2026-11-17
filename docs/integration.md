# Agent 接入协议

## 认证和输入

PrinterHttp每条独立用例使用新的httpx会话：GET `/api/auth/csrf` → POST `/api/auth/login`表单 → 再取CSRF → GET本人`/api/orders/{id}` → POST `/api/conversations` → POST唯一`turnId`的`/messages`。所有写请求携带刷新后的CSRF；无权设备直接记录拒绝，不自动替换账号或设备。

模型只接收显式问题和`preconditions`用户轮次。`expected_answer`、`expected_tools`参数即使提供也不进入请求。多轮同一咨询按顺序提交，每个前置轮结果独立保留，目标轮另有唯一标识；前置技术失败停止。HTTP失败、超时或结果未知不会自动重发Agent请求。

## 先保存回答，再读取观测

`PrinterHttpAdapter.run(question, save_answer, order_id=...)`强制传入保存回调。收到回答后先调用回调，成功才GET `/api/evaluation-observations/{traceId}`。观测总等待默认10秒、间隔500毫秒，单用例默认360秒、单请求195秒、响应体2MiB；这些是保护配置，不是实测性能。

保存失败直接停止网络后续调用。观测迟到、缺失、过期或写失败只改变观测可用性，原回答仍可读取。补充旧观测只能GET原trace，不能补跑模型、业务或从最终回答反推工具。后台执行器将回答、观测和评分分别提交到数据库；进程中断后，已提交的回答仍可查询。批次启动与恢复说明见 [架构说明](architecture.md)。

源端camelCase JSON导入后统一为Python snake_case。traceId、conversationId、turnId、orderId必须与已保存回答逐项一致；事件序号和eventId必须有序唯一。完整性按轨迹、工具结果、证据和用量分别保留，字段过滤不伪装成完整正文。

| 字段 | 含义 |
|---|---|
| `tools=null` | 工具未知；不能认定漏调用 |
| `tools=[]`且完整 | 证明没有工具调用 |
| `CHAT_TOOL` / `SERVER_COORDINATOR` | 模型选择的工具 / 服务端协调动作，分开保存 |
| `ACTUAL_MODEL_CONTEXT` | 采自实际模型请求包含的工具结果证据 |
| `CITATION_SUBSET` | 仅最终引用子集，不等于完整检索上下文 |
| `NOT_USED` / `UNAVAILABLE` | 确定未使用 / 无法取得，不能混用 |
| `context_completeness` | COMPLETE、PARTIAL或UNAVAILABLE；独立于范围 |
| `usage.status` | COMPLETE、PARTIAL、UNAVAILABLE、NOT_REQUESTED；未知计数保持null |
| `replayed` | 已保存轮次回放；不重复记模型用量，原trace未保留则说明原因 |

工具正文来自执行时返回值的白名单，排除工具参数、联系方式、认证信息和私人历史。实际模型输入证据可能大于最终答案引用集合。记录存在并不证明回答正确，完整实际证据也不等于独立人工验证。

## 通用HTTP与Mock

通用HTTP向配置地址POST `{execution_id, question, preconditions}`，最少返回`{answer: string}`，可选tools、tools_completeness、contexts、context_scope、context_completeness、usage及observed_version。必需字段缺失或规范字段类型非法产生协议错误；额外顶层字段仅脱敏保留在raw_agent，嵌套规范模型保持严格校验。不从HTTP 200自动推断模型、工具成功。Mock始终为`MOCK`来源，HTTP协议测试使用`PROTOCOL_DOUBLE`来源，实际远程连接仍需模型尝试元数据才能提升执行证据可信度。

## 源端安全和兼容

源端读取只允许已认证CUSTOMER，身份来自SessionUser，按可信客户复核咨询、轮次和本人设备；客户端owner/role查询参数不参与授权。UUID规范解析、私有根和文件类型检查防止路径逃逸。读取不调用模型、检索或写入业务数据，no-store禁止缓存私人正文。

原业务响应、CSRF、预算和页面确认不变。默认关闭时不新增正文记录；运行器清理继承的`EVALUATION_`变量，只有指定实例的私有env显式开启。私有存储故障只影响观测，不能回滚成功业务或放宽授权。

## 版本和用量

实际进程JAR SHA-256与实例固定包SHA-256核对；开发目录无法核验则UNKNOWN。稳定系统模板摘要、实际请求/响应模型标识与本轮知识文档版本分别保存。供应商模型别名的不可变快照、全知识库身份和外部价格没有依据时保持未知。Token只取真实模型返回元数据，未返回不补零；Chat与Embedding/Rerank的覆盖范围需分别说明，不把已知Chat用量称为供应商全部账单。

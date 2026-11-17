export const metricNames: Record<string, string> = { correctness: '正确性', relevance: '相关性', task_completion: '任务完成度', faithfulness: '依据充分性', execution_completed: '执行完成', answer_nonempty: '回答非空', expected_tools: '预期工具', tool_names: '工具名称集合', tool_success: '工具成功', response_time: '响应时间' }
export const categoryNames: Record<string, string> = { ANSWER_INCORRECT: '回答错误', ANSWER_IRRELEVANT: '回答不相关', TASK_INCOMPLETE: '任务未完成', EXPECTED_TOOL_MISSING: '预期工具未调用', TOOL_FAILED: '工具失败', AGENT_ERROR: 'Agent 异常', EXECUTION_TIMEOUT: '执行超时' }
export const sourceNames: Record<string, string> = { MOCK: '模拟', PROTOCOL_DOUBLE: '协议替身', REAL_EXTERNAL: '真实外部连接声明' }
export const stateNames: Record<string, string> = { FROZEN: '仅冻结', QUEUED: '排队中', RUNNING: '运行中', COMPLETED: '处理完成', FAILED: '批次故障', INTERRUPTED: '已中断', PASS: '通过', FAIL: '失败', UNDETERMINED: '未判定', SUCCEEDED: '执行成功', TIMEOUT: '执行超时', ERROR: '评分故障', NA: '不适用', PENDING: '待处理' }
export function number(value: number | null | undefined, digits = 2): string { return value == null ? '不可用' : Number.isInteger(value) ? String(value) : value.toFixed(digits) }
export function percent(value: number | null | undefined): string { return value == null ? '不可用' : `${(value * 100).toFixed(1)}%` }
export function pretty(value: unknown): string { return value == null ? '未提供' : typeof value === 'string' ? value : JSON.stringify(value, null, 2) }
export function date(value: string | null | undefined): string { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '未记录' }
export function query(value: unknown): string { return typeof value === 'string' ? value : '' }
export function jsonObject(text: string, label: string): Record<string, unknown> {
  let result: unknown
  try { result = JSON.parse(text) } catch { throw new Error(`${label}须为合法 JSON 对象。`) }
  if (!result || typeof result !== 'object' || Array.isArray(result)) throw new Error(`${label}须为 JSON 对象。`)
  return result as Record<string, unknown>
}
export async function allPages<T>(read: (page: number) => Promise<{ items: T[]; total: number; page_size: number }>): Promise<T[]> {
  const first = await read(1), result = [...first.items]
  for (let page = 2; (page - 1) * first.page_size < first.total; page++) result.push(...(await read(page)).items)
  return result
}

import axios from 'axios'
import type { AgentConfig, AgentInput, ApiProblem, CaseInput, Dataset, DatasetInput, Page, TestCase, Experiment, ExperimentInput, Run, ScoreSet, Evaluation, CaseDetail, Annotation, Comparison, Report, Dashboard } from '../types'

const client = axios.create({ baseURL: '/api/v1', timeout: 20000 })
export function problem(error: unknown): ApiProblem {
  if (axios.isAxiosError(error)) {
    const body = error.response?.data
    if (body && typeof body === 'object' && typeof body.code === 'string' && typeof body.message === 'string')
      return { code: body.code, message: body.message, trace_id: typeof body.trace_id === 'string' ? body.trace_id : undefined, fields: Array.isArray(body.fields) ? body.fields : [] }
    return { code: error.code === 'ECONNABORTED' ? 'REQUEST_TIMEOUT' : 'CONNECTION_ERROR', message: '未能取得后端响应。若刚执行保存或删除，请先刷新核对结果，再决定是否重试。', fields: [] }
  }
  return { code: 'INPUT_ERROR', message: error instanceof Error ? error.message : '请检查输入内容。', fields: [] }
}
export function validation(fields: { field: string; message: string }[]): ApiProblem | null {
  return fields.length ? { code: 'INPUT_ERROR', message: '请修正以下字段后再保存。', fields: fields.map(item => ({ ...item, code: 'invalid' })) } : null
}
export const api = {
  datasets: async (page = 1) => (await client.get<Page<Dataset>>('/datasets', { params: { page, page_size: 20 } })).data,
  dataset: async (id: number) => (await client.get<Dataset>(`/datasets/${id}`)).data,
  saveDataset: async (input: DatasetInput, id?: number) => (await (id ? client.patch<Dataset>(`/datasets/${id}`, input) : client.post<Dataset>('/datasets', input))).data,
  deleteDataset: async (id: number) => { await client.delete(`/datasets/${id}`) },
  cases: async (id: number, page = 1, enabled?: boolean, tag?: string) => (await client.get<Page<TestCase>>(`/datasets/${id}/cases`, { params: { page, page_size: 20, enabled, tag: tag || undefined } })).data,
  saveCase: async (id: number, input: CaseInput, key?: string) => {
    const { case_key, ...patch } = input
    return (await (key ? client.patch<TestCase>(`/datasets/${id}/cases/${encodeURIComponent(key)}`, patch) : client.post<TestCase>(`/datasets/${id}/cases`, { case_key, ...patch }))).data
  },
  toggleCase: async (id: number, key: string, enabled: boolean) => { await client.patch(`/datasets/${id}/cases/${encodeURIComponent(key)}`, { enabled }) },
  deleteCase: async (id: number, key: string) => { await client.delete(`/datasets/${id}/cases/${encodeURIComponent(key)}`) },
  agents: async (page = 1) => (await client.get<Page<AgentConfig>>('/agent-configs', { params: { page, page_size: 20 } })).data,
  saveAgent: async (input: AgentInput, id?: number) => (await (id ? client.patch<AgentConfig>(`/agent-configs/${id}`, input) : client.post<AgentConfig>('/agent-configs', input))).data,
  deleteAgent: async (id: number) => { await client.delete(`/agent-configs/${id}`) },
  experiments: async (page = 1) => (await client.get<Page<Experiment>>('/experiments', { params: { page, page_size: 20 } })).data,
  experiment: async (id: number) => (await client.get<Experiment>(`/experiments/${id}`)).data,
  saveExperiment: async (input: ExperimentInput, id?: number) => (await (id ? client.patch<Experiment>(`/experiments/${id}`, input) : client.post<Experiment>('/experiments', input))).data,
  deleteExperiment: async (id: number) => { await client.delete(`/experiments/${id}`) },
  runs: async (page = 1, experimentId?: number) => (await client.get<Page<Run>>(experimentId ? `/experiments/${experimentId}/runs` : '/runs', { params: { page, page_size: 20 } })).data,
  run: async (id: string) => (await client.get<Run>(`/runs/${id}`)).data,
  startExperiment: async (id: number, input: Record<string, unknown>) => (await client.post<{ run_id: string; status: string }>(`/experiments/${id}/runs`, input)).data,
  scoreSets: async (id: string, page = 1) => (await client.get<Page<ScoreSet>>(`/runs/${id}/score-sets`, { params: { page, page_size: 100 } })).data,
  scoreSet: async (id: string) => (await client.get<ScoreSet>(`/score-sets/${id}`)).data,
  rescore: async (input: Record<string, unknown>) => (await client.post<ScoreSet>('/score-sets', input)).data,
  evaluations: async (params: Record<string, string | number | undefined>) => (await client.get<Page<Evaluation>>('/evaluations', { params: { page_size: 20, ...params } })).data,
  detail: async (id: string, scoreSetId?: string) => (await client.get<CaseDetail>(`/case-runs/${id}/detail`, { params: { score_set_id: scoreSetId || undefined } })).data,
  badcases: async (params: Record<string, string | number | undefined>) => (await client.get<Page<Evaluation>>('/badcases', { params: { page_size: 20, ...params } })).data,
  saveAnnotation: async (id: string, scoreSetId: string, input: Annotation) => (await client.put<Annotation>(`/case-runs/${id}/annotation`, { score_set_id: scoreSetId, manual_tags: input.manual_tags, note: input.note })).data,
  comparison: async (left: string, right: string, leftScore?: string, rightScore?: string) => (await client.get<Comparison>('/comparisons', { params: { left_run_id: left, right_run_id: right, left_score_set_id: leftScore || undefined, right_score_set_id: rightScore || undefined } })).data,
  report: async (id: string, scoreSetId?: string, compareRun?: string, compareScore?: string) => (await client.get<Report>(`/reports/${id}`, { params: { score_set_id: scoreSetId || undefined, compare_run_id: compareRun || undefined, compare_score_set_id: compareScore || undefined } })).data,
  dashboard: async (source = 'REAL_EXTERNAL') => (await client.get<Dashboard>('/dashboard', { params: { source_type: source } })).data,
}

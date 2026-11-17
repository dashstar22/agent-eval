import axios from 'axios'
import type { Consistency, HumanReview, RegressionCheck, RegressionDraft, RegressionPolicy, RepeatBatch, ReviewCase, ReviewHistory, ReviewInput, Selection, StabilityAnalysis } from '../types/extensions'

const client = axios.create({ baseURL: '/api/v1/extensions', timeout: 30000 })
export const extensions = {
  reviewCase: async (id: string, score?: string, blind = true) => (await client.get<ReviewCase>(`/reviews/cases/${id}`, { params: { score_set_id: score || undefined, blind } })).data,
  reviewCases: async (run: string, score?: string) => (await client.get<{ items: { id: string; case_key: string; question: string }[]; run_id: string; score_set_id: string }>('/reviews/cases', { params: { run_id: run, score_set_id: score || undefined } })).data,
  saveReview: async (input: ReviewInput) => (await client.post<HumanReview>('/reviews', input)).data,
  reviews: async (run: string, score?: string) => (await client.get<ReviewHistory>('/reviews', { params: { run_id: run, score_set_id: score || undefined } })).data,
  consistency: async (run: string, score?: string) => (await client.get<Consistency>('/reviews/consistency', { params: { run_id: run, score_set_id: score || undefined } })).data,
  drafts: async (dataset?: number) => (await client.get<{ items: RegressionDraft[] }>('/regression-drafts', { params: { target_dataset_id: dataset } })).data,
  createDraft: async (input: { case_run_id: string; score_set_id: string; target_dataset_id: number; case_key: string; expected_answer: string; reason: string }) => (await client.post<RegressionDraft>('/regression-drafts', input)).data,
  approveDraft: async (id: string, reviewer: string) => (await client.post<RegressionDraft>(`/regression-drafts/${id}/approve`, { reviewer_name: reviewer, human_confirmed: true })).data,
  regression: async (baseline: Selection, candidate: Selection, policy: RegressionPolicy) => (await client.post<RegressionCheck>('/regression-checks', { baseline, candidate, policy })).data,
  stability: async (selections: Selection[]) => (await client.post<StabilityAnalysis>('/stability-analysis', { selections })).data,
  repeat: async (source: string, count: number, confirmed: boolean, requestId: string) => (await client.post<RepeatBatch>('/repeat-batches', { request_id: requestId, source_run_id: source, repeat_count: count, confirm_real_execution: confirmed })).data,
  batches: async () => (await client.get<{ items: RepeatBatch[] }>('/repeat-batches')).data,
  batch: async (id: string) => (await client.get<RepeatBatch>(`/repeat-batches/${id}`)).data,
  exportReport: async (run: string, score: string, format: 'json' | 'csv' | 'html') => {
    try {
      const result = await client.get<Blob>(`/reports/${run}/export`, { params: { score_set_id: score, format }, responseType: 'blob' })
      const url = URL.createObjectURL(result.data), anchor = document.createElement('a')
      anchor.href = url; anchor.download = `agenteval-${run}-${score}.${format}`; document.body.append(anchor); anchor.click(); anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (error) {
      if (axios.isAxiosError(error) && error.response?.data instanceof Blob) {
        try { error.response.data = JSON.parse(await error.response.data.text()) } catch { /* Keep the original transport error. */ }
      }
      throw error
    }
  },
}

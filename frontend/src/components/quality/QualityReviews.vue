<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { problem, validation } from '../../api'
import { extensions } from '../../api/extensions'
import type { ApiProblem } from '../../types'
import type { Consistency, QualityConclusion, ReviewCase, ReviewHistory } from '../../types/extensions'
import { date, metricNames, number, percent, pretty } from '../../workbench'
import AsyncState from '../AsyncState.vue'
import EvidenceNotice from '../EvidenceNotice.vue'
import ProblemBox from '../ProblemBox.vue'
import SectionCard from '../SectionCard.vue'
import StatusBadge from '../StatusBadge.vue'

const props = defineProps<{ runId: string; scoreSetId: string; caseRunId: string }>()
const emit = defineEmits<{ selectCase: [id: string] }>()
const choices = ref<{ id: string; case_key: string; question: string }[]>([]), choicesLoading = ref(false), choicesError = ref<ApiProblem | null>(null)
const detail = ref<ReviewCase | null>(null), loading = ref(false), error = ref<ApiProblem | null>(null), formError = ref<ApiProblem | null>(null)
const saving = ref(false), confirmed = ref(false), saved = ref(false), notice = ref(''), revealed = ref(false), revealing = ref(false)
const comparisonsSeen = ref(false)
const reviewer = ref(''), conclusion = ref<QualityConclusion>('UNDETERMINED'), reason = ref(''), scores = reactive<Record<string, string>>({})
const history = ref<ReviewHistory | null>(null), consistency = ref<Consistency | null>(null), historyBusy = ref(false), historyError = ref<ApiProblem | null>(null)
const ratingMetrics = computed(() => detail.value?.applicable_metrics.filter(item => ['correctness', 'relevance', 'task_completion', 'faithfulness'].includes(item)) || [])
let contextGeneration = 0, detailGeneration = 0, historyGeneration = 0, disposed = false
async function loadChoices() {
  const generation = ++contextGeneration
  choices.value = []; choicesError.value = null; choicesLoading.value = Boolean(props.runId)
  history.value = null; consistency.value = null; historyError.value = null; historyGeneration++
  comparisonsSeen.value = false
  if (!props.runId) return
  try { const result = await extensions.reviewCases(props.runId, props.scoreSetId); if (!disposed && generation === contextGeneration) choices.value = result.items }
  catch (e) { if (!disposed && generation === contextGeneration) choicesError.value = problem(e) }
  finally { if (!disposed && generation === contextGeneration) choicesLoading.value = false }
}
async function loadCase() {
  const generation = ++detailGeneration
  detail.value = null; error.value = null; formError.value = null; notice.value = ''; confirmed.value = false; saved.value = false; revealed.value = false; revealing.value = false; saving.value = false
  conclusion.value = 'UNDETERMINED'; reason.value = ''; Object.keys(scores).forEach(key => delete scores[key]); loading.value = Boolean(props.caseRunId && props.runId)
  if (!props.caseRunId || !props.runId) return
  try {
    // This dedicated endpoint never returns Judge fields in blind mode.
    const result = await extensions.reviewCase(props.caseRunId, props.scoreSetId, true)
    if (disposed || generation !== detailGeneration) return
    if (result.case.run_id !== props.runId) throw new Error('该用例不属于当前运行，请重新选择。')
    detail.value = result
  } catch (e) { if (!disposed && generation === detailGeneration) error.value = problem(e) }
  finally { if (!disposed && generation === detailGeneration) loading.value = false }
}
async function save() {
  if (!detail.value || saving.value) return
  const target = detail.value, generation = detailGeneration
  formError.value = validation([
    ...(!reviewer.value.trim() ? [{ field: 'reviewer_name', message: '填写本次复核人的姓名或本机标识。' }] : []),
    ...(!reason.value.trim() ? [{ field: 'reason', message: '填写判定理由。' }] : []),
    ...(!confirmed.value ? [{ field: 'human_confirmed', message: '请人工核对内容并勾选确认。' }] : []),
  ])
  if (formError.value) return
  const inputScores = Object.fromEntries(ratingMetrics.value.filter(metric => scores[metric]).map(metric => [metric, Number(scores[metric])]))
  saving.value = true; notice.value = ''
  try {
    await extensions.saveReview({ case_run_id: target.case.id, score_set_id: target.score_set.id, reviewer_name: reviewer.value.trim(), conclusion: conclusion.value, scores: inputScores, reason: reason.value.trim(), human_confirmed: true })
    if (disposed || generation !== detailGeneration) return
    saved.value = true; confirmed.value = false; notice.value = '人工复核已追加保存。Judge、原回答与历史复核保留。'
    if (history.value) void loadHistory()
  } catch (e) { if (!disposed && generation === detailGeneration) formError.value = problem(e) }
  finally { if (!disposed && generation === detailGeneration) saving.value = false }
}
async function reveal() {
  if (!detail.value || !saved.value || revealing.value) return
  const generation = detailGeneration, target = detail.value
  revealing.value = true; formError.value = null
  try { const result = await extensions.reviewCase(target.case.id, target.score_set.id, false); if (!disposed && generation === detailGeneration) { detail.value = result; revealed.value = true } }
  catch (e) { if (!disposed && generation === detailGeneration) formError.value = problem(e) }
  finally { if (!disposed && generation === detailGeneration) revealing.value = false }
}
async function loadHistory() {
  if (!props.runId || historyBusy.value) return
  const generation = ++historyGeneration
  historyBusy.value = true; historyError.value = null; comparisonsSeen.value = true
  try { const [records, analysis] = await Promise.all([extensions.reviews(props.runId, props.scoreSetId), extensions.consistency(props.runId, props.scoreSetId)]); if (!disposed && generation === historyGeneration) { history.value = records; consistency.value = analysis } }
  catch (e) { if (!disposed && generation === historyGeneration) historyError.value = problem(e) }
  finally { if (!disposed && generation === historyGeneration) historyBusy.value = false }
}
watch(() => [props.runId, props.scoreSetId], () => { historyBusy.value = false; void loadChoices() }, { immediate: true })
watch(() => [props.runId, props.scoreSetId, props.caseRunId], loadCase, { immediate: true })
onBeforeUnmount(() => { disposed = true; contextGeneration++; detailGeneration++; historyGeneration++ })
</script>

<template>
  <div class="quality-stack">
    <SectionCard title="独立判断后，再查看 Judge" description="先读冻结问题、参考行为、回答与上下文，保存自己的结论。">
      <AsyncState :loading="choicesLoading" :error="choicesError" :empty="!runId || !choices.length" :empty-title="!runId ? '先选择来源运行' : '该运行尚无可复核用例'" empty-text="可从评测详情进入指定用例；复核不会触发模型调用。" @retry="loadChoices">
        <label class="quality-field">待复核用例<select :value="caseRunId" :disabled="saving" @change="emit('selectCase', ($event.target as HTMLSelectElement).value)"><option value="">选择用例开始盲审</option><option v-if="caseRunId && !choices.some(item => item.id === caseRunId)" :value="caseRunId">深链用例 · {{ caseRunId }}</option><option v-for="item in choices" :key="item.id" :value="item.id">{{ item.case_key }} · {{ item.question.slice(0, 90) }}</option></select></label>
      </AsyncState>
      <AsyncState v-if="runId && caseRunId" :loading="loading" :error="error" @retry="loadCase">
        <div v-if="detail" class="quality-stack" style="margin-top:var(--space-5)">
          <EvidenceNotice :title="revealed || comparisonsSeen ? '已查看评分对照 · 当前为对照复核' : '盲审模式 · 本题数据不返回 Judge 结论'" :description="detail.identity_notice" :tone="revealed || comparisonsSeen ? 'warning' : 'info'" />
          <div class="quality-id">{{ detail.case.case_key }} · {{ detail.case.id }} · 评分集 {{ detail.score_set.id }}</div>
          <div class="quality-reading"><section><h3>冻结问题</h3><p class="preserve-text">{{ detail.case.question }}</p><h3 style="margin-top:var(--space-5)">参考行为</h3><p class="preserve-text">{{ detail.case.expected_answer }}</p></section><section><h3>实际回答</h3><p class="preserve-text quality-answer">{{ detail.case.answer === null ? '尚无回答' : detail.case.answer || '回答为空字符串' }}</p></section></div>
          <details class="raw-detail"><summary>上下文、工具预期与前置条件</summary><p class="small muted">范围 {{ detail.case.context_scope }} · 完整性 {{ detail.case.context_completeness }}；引用子集不能视为完整上下文。</p><pre>{{ pretty({ contexts: detail.case.contexts, expected_tools: detail.case.expected_tools, tool_match_mode: detail.case.tool_match_mode, tool_assertions: detail.case.tool_assertions, preconditions: detail.case.preconditions }) }}</pre></details>
          <form class="quality-form" @submit.prevent="save">
            <div v-if="notice" class="notice" role="status">{{ notice }}</div><ProblemBox :error="formError" />
            <div class="quality-fields"><label>复核人标识<input v-model="reviewer" maxlength="100" autocomplete="name" :disabled="saving" placeholder="本机填报身份，未经认证" /></label><label>人工整体结论<select v-model="conclusion" :disabled="saving"><option value="PASS">通过</option><option value="FAIL">失败</option><option value="UNDETERMINED">未判定</option></select></label></div>
            <div v-if="ratingMetrics.length" class="quality-fields"><label v-for="metric in ratingMetrics" :key="metric">{{ metricNames[metric] || metric }}（可留空）<select v-model="scores[metric]" :disabled="saving"><option value="">未评分 / 证据不足</option><option v-for="value in 5" :key="value" :value="String(value)">{{ value }} / 5</option></select></label></div>
            <label>判定理由<textarea v-model="reason" rows="4" maxlength="20000" :disabled="saving" placeholder="说明回答如何满足或不满足参考行为，并引用可核对的证据。" /></label>
            <label class="quality-check"><input v-model="confirmed" type="checkbox" :disabled="saving" />我已人工阅读并核对本题、分数与理由，确认提交本次复核。</label>
            <div class="quality-actions"><ElButton native-type="submit" type="primary" :loading="saving">保存人工复核</ElButton><ElButton :disabled="!saved || revealed || saving" :loading="revealing" @click="reveal">保存后查看 Judge 对照</ElButton></div>
          </form>
          <section v-if="revealed && detail.judge" class="quality-record"><div class="quality-record-title"><h3>已保存 Judge 对照</h3><StatusBadge :value="detail.judge.conclusion" domain="quality" /></div><div class="table-scroll"><table><thead><tr><th>指标</th><th>判定</th><th>分数</th><th>理由</th></tr></thead><tbody><tr v-for="metric in detail.judge.metrics" :key="metric.metric"><td>{{ metricNames[metric.metric] || metric.metric }}</td><td>{{ metric.status }}</td><td>{{ number(metric.score) }}</td><td class="preserve-text">{{ metric.reason }}</td></tr></tbody></table></div></section>
        </div>
      </AsyncState>
    </SectionCard>
    <SectionCard title="复核记录与一致性" description="每题使用最新人工复核；未知或漏评不按 0 计入有效配对。">
      <template #actions><ElButton :disabled="!runId" :loading="historyBusy" @click="loadHistory">读取复核与一致性</ElButton></template>
      <ProblemBox :error="historyError" />
      <p v-if="!history" class="muted small">完成独立复核后，可主动读取记录与已保存评测的对照。</p>
      <div v-else class="quality-stack"><EvidenceNotice title="本机填报复核记录" :description="history.identity_notice" tone="warning" /><div v-if="consistency" class="quality-facts"><span>复核记录 <strong>{{ consistency.review_count }}</strong></span><span>已复核题 <strong>{{ consistency.latest_review_count }}</strong></span><span>结论有效配对 <strong>{{ consistency.conclusions.valid_pair_count }}</strong></span><span>一致 <strong>{{ consistency.conclusions.agreement_count }}</strong> · {{ percent(consistency.conclusions.agreement_rate) }}</span></div>
        <div v-if="consistency?.metrics.length" class="table-scroll"><table><thead><tr><th>Judge 指标</th><th>有效配对</th><th>精确一致</th><th>一致率</th><th>平均绝对分差</th></tr></thead><tbody><tr v-for="metric in consistency.metrics" :key="metric.metric"><td>{{ metricNames[metric.metric] || metric.metric }}</td><td>{{ metric.valid_pair_count }}</td><td>{{ metric.exact_agreement_count }}</td><td>{{ percent(metric.exact_agreement_rate) }}</td><td>{{ number(metric.mean_absolute_difference) }}</td></tr></tbody></table></div>
        <details v-if="consistency" class="raw-detail"><summary>结论与指标分歧题</summary><pre>{{ pretty({ conclusions: consistency.conclusions.disagreements, metrics: consistency.metrics.map(metric => ({ metric: metric.metric, disagreements: metric.disagreements })) }) }}</pre></details>
        <p v-if="!history.items.length" class="small muted">当前评分集尚无人工复核。选择上方用例并保存第一条记录。</p><div v-for="record in history.items" :key="record.id" class="quality-record"><div class="quality-record-title"><strong>{{ record.reviewer_name }} · {{ date(record.created_at) }}</strong><StatusBadge :value="record.conclusion" domain="quality" /></div><p class="quality-id">Case {{ record.case_run_id }}</p><p class="preserve-text">{{ record.reason }}</p><p class="small">{{ Object.entries(record.scores).map(([metric, value]) => `${metricNames[metric] || metric}: ${value}`).join(' · ') || '未填指标分数' }}</p><RouterLink class="text-link small" :to="{ path: '/evaluations', query: { run_id: record.run_id, score_set_id: record.score_set_id, case_run_id: record.case_run_id } }">查看原始证据 →</RouterLink></div>
      </div>
    </SectionCard>
  </div>
</template>

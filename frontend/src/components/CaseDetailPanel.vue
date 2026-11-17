<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api, problem, validation } from '../api'
import type { ApiProblem, CaseDetail } from '../types'
import { categoryNames, metricNames, number, pretty, sourceNames } from '../workbench'
import AsyncState from './AsyncState.vue'
import EvidenceNotice from './EvidenceNotice.vue'
import ProblemBox from './ProblemBox.vue'
import StatusBadge from './StatusBadge.vue'
import VersionPanel from './VersionPanel.vue'

const props = defineProps<{ caseId: string; scoreSetId?: string }>()
const emit = defineEmits<{ scoreChange: [scoreSet: string]; saved: [] }>()
const detail = ref<CaseDetail | null>(null), loading = ref(true), error = ref<ApiProblem | null>(null)
const formError = ref<ApiProblem | null>(null), saving = ref(false), tags = ref(''), note = ref(''), notice = ref('')
const execution = computed(() => (detail.value?.case.raw_agent?.normalized_execution || {}) as Record<string, unknown>)
const source = computed(() => String(execution.value.source_type || '尚无执行来源'))
const modelVerified = computed(() => execution.value.model_execution_verified === true)
const quality = computed(() => detail.value?.scoring.evaluation?.conclusion || 'UNDETERMINED')
const attentionMetrics = computed(() => detail.value?.scoring.metrics.filter(metric =>
  metric.status === 'FAIL' || metric.status === 'ERROR' || (metric.required && metric.status === 'NA'),
) || [])
let generation = 0, saveSequence = 0, disposed = false

async function load() {
  const current = ++generation, requestedCase = props.caseId, requestedScore = props.scoreSetId
  loading.value = true
  error.value = null
  formError.value = null
  detail.value = null
  notice.value = ''
  tags.value = ''
  note.value = ''
  saving.value = false
  try {
    const data = await api.detail(requestedCase, requestedScore)
    if (disposed || current !== generation) return
    detail.value = data
    tags.value = data.annotation.manual_tags.join('、')
    note.value = data.annotation.note
  } catch (e) {
    if (!disposed && current === generation) error.value = problem(e)
  } finally {
    if (!disposed && current === generation) loading.value = false
  }
}

async function save() {
  if (!detail.value || saving.value) return
  const target = detail.value, current = generation, request = ++saveSequence
  const targetCase = target.case.id, targetScore = target.score_set.id
  const items = tags.value.split(/[、,，\n]/).map(x => x.trim()).filter(Boolean)
  formError.value = validation([
    ...(items.length > 30 ? [{ field: 'manual_tags', message: '最多 30 个原因' }] : []),
    ...(new Set(items).size !== items.length ? [{ field: 'manual_tags', message: '原因不能重复' }] : []),
    ...(items.some(x => x.length > 200) ? [{ field: 'manual_tags', message: '每个原因最多 200 字' }] : []),
    ...(note.value.length > 20000 ? [{ field: 'note', message: '备注最多 20000 字' }] : []),
  ])
  if (formError.value) return
  const input = { manual_tags: items, note: note.value }
  const stillCurrent = () => !disposed && current === generation && request === saveSequence && detail.value === target && props.caseId === targetCase
  saving.value = true
  notice.value = ''
  try {
    const annotation = await api.saveAnnotation(targetCase, targetScore, input)
    if (!stillCurrent()) return
    target.annotation = annotation
    tags.value = annotation.manual_tags.join('、')
    note.value = annotation.note
    notice.value = '人工原因与备注已保存，原评分和 Judge 理由保留。'
    emit('saved')
  } catch (e) {
    if (stillCurrent()) formError.value = problem(e)
  } finally {
    if (stillCurrent()) saving.value = false
  }
}

watch(() => [props.caseId, props.scoreSetId], () => { void load() }, { immediate: true })
onBeforeUnmount(() => { disposed = true; generation++; saveSequence++ })
</script>

<template>
  <section class="panel case-detail-panel">
    <div class="panel-heading"><h2>冻结用例详情</h2><ElButton :disabled="saving" @click="load">刷新详情</ElButton></div>
    <AsyncState :loading="loading" :error="error" @retry="load">
      <div v-if="detail" class="panel-body case-detail-body">
        <div class="case-heading">
          <div><div class="case-heading-title"><h2 class="mono">{{ detail.case.case_key }}</h2><StatusBadge :value="quality" domain="quality" /></div><p class="case-identifiers">Run {{ detail.case.run_id.slice(0, 8) }} · Case {{ detail.case.id.slice(0, 8) }}</p></div>
          <label class="case-score-picker">评分集<select :value="detail.score_set.id" :disabled="saving" @change="emit('scoreChange', ($event.target as HTMLSelectElement).value)"><option v-for="item in detail.score_sets" :key="item.id" :value="item.id">{{ item.kind }} · {{ item.status }} · {{ item.id.slice(0, 8) }}</option></select></label>
        </div>
        <div class="case-state-axes"><span>执行 <StatusBadge :value="detail.case.agent_status" domain="execution" /></span><span>评分 <StatusBadge :value="detail.scoring.evaluation?.status" domain="scoring" /></span><span>阶段 <span class="mono">{{ detail.case.phase }}</span></span></div>
        <div v-if="detail.automatic_tags.length || detail.evaluation_issues.length" class="tag-list"><ElTag v-for="tag in detail.automatic_tags" :key="tag" type="danger" size="small">{{ categoryNames[tag] || tag }}</ElTag><ElTag v-for="item in detail.evaluation_issues" :key="item" type="warning" size="small">评测异常：{{ item }}</ElTag></div>

        <section v-if="attentionMetrics.length || quality !== 'PASS'" class="case-verdict" :class="{ 'case-verdict--warning': quality !== 'FAIL' }">
          <h3>{{ quality === 'FAIL' ? '失败指标与已保存理由' : quality === 'PASS' ? '指标提醒与已保存理由' : '未判定与评分异常' }}</h3>
          <div v-for="metric in attentionMetrics" :key="metric.metric" class="verdict-metric">
            <div class="verdict-metric-title"><strong>{{ metricNames[metric.metric] || metric.metric }}</strong><StatusBadge :value="metric.status" :domain="metric.status === 'ERROR' ? 'scoring' : 'quality'" /><span class="small">{{ metric.required ? '必评' : '可选' }} · 分数 {{ number(metric.score) }}</span></div>
            <p class="preserve-text">{{ metric.reason }}</p>
            <details class="raw-detail"><summary>查看这项判定依据</summary><pre>{{ pretty(metric.evidence) }}</pre></details>
          </div>
          <p v-if="!attentionMetrics.length" class="preserve-text">该评分集尚无可展示的失败指标理由。请结合评分状态、评测异常和原始记录核对，不能由缺少理由推断通过。</p>
        </section>

        <EvidenceNotice
          :title="`${sourceNames[source] || source} · ${modelVerified ? '实际模型已核验' : '实际模型未核验'}`"
          :tone="source === 'REAL_EXTERNAL' && modelVerified ? 'info' : 'warning'"
          description="执行来源、实际模型核验与质量结论分别展示；上下文和工具观测的完整性见下方证据。"
        />

        <div class="case-reading-grid">
          <section><h3>原问题</h3><p class="preserve-text">{{ detail.case.case_snapshot.question }}</p><div class="case-reference"><h3>冻结参考行为</h3><p class="preserve-text">{{ detail.case.case_snapshot.expected_answer }}</p></div><details class="raw-detail"><summary>预期工具、必评集合与前置条件</summary><pre>{{ pretty(detail.case.case_snapshot) }}</pre></details></section>
          <section><h3>Agent 实际回答</h3><p class="preserve-text case-answer">{{ detail.case.answer === null ? '尚未取得回答' : detail.case.answer || '回答为空字符串' }}</p><dl class="fact-list"><dt>Trace</dt><dd class="mono">{{ detail.case.trace_id || '不可用' }}</dd><dt>计时</dt><dd class="mono">{{ pretty(detail.case.timing) }}</dd><dt>错误</dt><dd class="mono">{{ pretty(detail.case.error) }}</dd></dl></section>
        </div>

        <details class="case-evidence-group" open>
          <summary>规则与 Judge 指标 · 全部判定与依据</summary>
          <div class="evidence-group-body"><div class="table-scroll"><table><thead><tr><th>指标 / 必评</th><th>判定</th><th>分数</th><th>理由与依据</th></tr></thead><tbody><tr v-for="metric in detail.scoring.metrics" :key="metric.metric"><td>{{ metricNames[metric.metric] || metric.metric }}<p class="table-subtext">{{ metric.required ? '必评' : '可选' }}</p></td><td><StatusBadge :value="metric.status" :domain="metric.status === 'ERROR' ? 'scoring' : 'quality'" /></td><td class="metric-score">{{ number(metric.score) }}</td><td><p class="preserve-text">{{ metric.reason }}</p><details class="raw-detail"><summary>判定依据</summary><pre>{{ pretty(metric.evidence) }}</pre></details></td></tr></tbody></table></div><p v-if="!detail.scoring.metrics.length" class="small muted">该评分集尚无已保存指标。</p></div>
        </details>

        <details class="case-evidence-group">
          <summary>按序工具轨迹 · {{ detail.case.observations?.tools_completeness || 'UNAVAILABLE' }}</summary>
          <div class="evidence-group-body"><p v-if="!detail.traces.length" class="muted small">{{ detail.case.observations?.tools_completeness === 'COMPLETE' ? '完整观测记录证明没有工具调用。' : '轨迹未知或尚未保存，不能据此认定没有调用。' }}</p><ol v-else class="trace-timeline"><li v-for="event in detail.traces" :key="event.event_id"><div class="trace-sequence">{{ event.sequence }}</div><div class="trace-content"><h3>{{ event.name }} <ElTag size="small" type="info">{{ event.source }}</ElTag></h3><p class="small muted">{{ event.status }} · {{ number(event.duration_ms) }} ms · {{ event.completeness }}</p><details class="raw-detail"><summary>实际工具结果</summary><pre>{{ pretty(event.payload ?? event.result) }}</pre></details></div></li></ol></div>
        </details>

        <details class="case-evidence-group">
          <summary>检索证据与模型用量</summary>
          <div class="evidence-group-body"><p class="small muted">上下文范围 {{ detail.case.observations?.context_scope || 'UNAVAILABLE' }} · 完整性 {{ detail.case.observations?.context_completeness || 'UNAVAILABLE' }}。引用子集不能视为完整模型上下文。</p><details class="raw-detail"><summary>查看实际观测与证据正文</summary><pre>{{ pretty(detail.case.observations) }}</pre></details><details class="raw-detail"><summary>Agent 用量与逐次尝试</summary><pre>{{ pretty(detail.case.usage) }}</pre></details></div>
        </details>

        <details class="case-evidence-group">
          <summary>Judge 原始尝试 · {{ detail.scoring.judge_attempts.length }} 次记录</summary>
          <div class="evidence-group-body"><div v-for="attempt in detail.scoring.judge_attempts" :key="attempt.attempt" class="judge-attempt"><h3>Judge 尝试 {{ attempt.attempt }} · {{ attempt.status }} <small>{{ number(attempt.duration_ms) }} ms</small></h3><details class="raw-detail"><summary>原始响应与解析理由</summary><pre>{{ pretty(attempt.response) }}</pre></details><details class="raw-detail"><summary>输入、用量与错误</summary><pre>{{ pretty({ request: attempt.request, usage: attempt.usage, error: attempt.error }) }}</pre></details></div><p v-if="!detail.scoring.judge_attempts.length" class="muted small">该评分集尚无模型尝试记录；没有调用不能视为评分成功。</p></div>
        </details>

        <details class="case-evidence-group">
          <summary>实际版本核验 · {{ detail.version_verification.status }}</summary>
          <div class="evidence-group-body"><VersionPanel :value="detail.version_verification" /></div>
        </details>

        <details class="case-evidence-group annotation-group" open>
          <summary>人工原因与备注</summary>
          <form class="evidence-group-body annotation-form" @submit.prevent="save">
            <p class="small muted">只写入当前评分集。人工复核、自动分类和 Judge 原理由各自保留。</p>
            <div v-if="notice" class="notice" role="status">{{ notice }}</div>
            <ProblemBox :error="formError" />
            <label>人工原因（用顿号分隔）<input v-model="tags" :disabled="saving" placeholder="例如：知识缺失、参考行为需复核" /></label>
            <label>人工备注<textarea v-model="note" :disabled="saving" rows="4" maxlength="20000" /></label>
            <div><ElButton native-type="submit" type="primary" :loading="saving">保存人工标记</ElButton></div>
            <p class="annotation-identity">评分集 {{ detail.score_set.id }} · {{ detail.annotation.updated_at || '尚未保存人工标记' }}</p>
          </form>
        </details>

        <details class="case-evidence-group"><summary>脱敏原始 Agent 记录与来源证明</summary><div class="evidence-group-body"><dl class="fact-list"><dt>Run</dt><dd class="mono">{{ detail.case.run_id }}</dd><dt>Case</dt><dd class="mono">{{ detail.case.id }}</dd><dt>评分集</dt><dd class="mono">{{ detail.score_set.id }}</dd></dl><div class="raw-detail"><pre>{{ pretty(detail.case.raw_agent) }}</pre></div></div></details>
      </div>
    </AsyncState>
  </section>
</template>

<style scoped>
.case-detail-panel { min-width: 0; }
.case-detail-panel > .panel-heading { padding: var(--space-4) var(--space-5); }
.case-detail-body { display: grid; gap: var(--space-6); }
.case-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: var(--space-4); flex-wrap: wrap; }
.case-heading-title { display: flex; gap: var(--space-3); align-items: center; flex-wrap: wrap; }
.case-heading-title h2 { font-size: 24px; }
.case-identifiers { color: var(--muted); font: var(--text-xs)/1.6 var(--font-code); margin-top: var(--space-2); }
.case-score-picker { display: grid; gap: var(--space-2); font-size: var(--text-xs); color: var(--muted); max-width: 100%; }
.case-score-picker select { min-height: var(--control-height); font-size: var(--text-xs); max-width: 260px; width: 100%; }
.case-state-axes { display: flex; gap: var(--space-3) var(--space-5); flex-wrap: wrap; font-size: var(--text-xs); color: var(--muted); }
.case-state-axes > span { display: flex; align-items: center; gap: var(--space-2); }
.case-verdict { padding: var(--space-4); border-left: 3px solid var(--danger); background: var(--danger-soft); border-radius: 0 var(--radius-control) var(--radius-control) 0; display: grid; gap: var(--space-4); }
.case-verdict > h3 { color: var(--danger); margin: 0; font-size: var(--text-md); }
.case-verdict--warning { border-left-color: var(--warning); background: var(--warning-soft); }
.case-verdict--warning > h3 { color: var(--warning); }
.verdict-metric + .verdict-metric { border-top: 1px solid var(--line); padding-top: var(--space-4); }
.verdict-metric-title { display: flex; gap: var(--space-2); align-items: center; flex-wrap: wrap; margin-bottom: var(--space-2); }
.verdict-metric .raw-detail { border-top: 0; margin-top: var(--space-2); padding-top: 0; }
.case-reading-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-6); }
.case-reading-grid > section { min-width: 0; }
.case-reading-grid h3 { font-size: var(--text-md); margin: 0 0 var(--space-3); }
.case-reference { padding-top: var(--space-4); margin-top: var(--space-4); border-top: 1px solid var(--line); color: var(--muted); }
.case-answer { padding: var(--space-4); background: var(--subtle); border-radius: var(--radius-control); }
.case-detail-panel .preserve-text { font-size: var(--text-sm); line-height: 1.8; }
.case-detail-panel .fact-list { font-size: var(--text-xs); }
.case-evidence-group { min-width: 0; border-top: 1px solid var(--line); }
.case-evidence-group > summary { padding: var(--space-4) 0; font-size: var(--text-sm); font-weight: 600; color: var(--ink); cursor: pointer; overflow-wrap: anywhere; }
.evidence-group-body { min-width: 0; }
.case-evidence-group table { min-width: 600px; }
.case-evidence-group th { font-size: var(--text-xs); }
.case-evidence-group td { padding: var(--space-4); font-size: var(--text-sm); }
.case-evidence-group td:last-child { min-width: 230px; }
.case-evidence-group .raw-detail { font-size: var(--text-xs); }
.case-evidence-group :deep(.version-panel) { margin-top: 0; padding-top: 0; border-top: 0; }
.metric-score { font-family: var(--font-data); font-variant-numeric: tabular-nums; white-space: nowrap; }
.annotation-group { background: var(--subtle); padding: 0 var(--space-4) var(--space-4); border: 1px solid var(--line); border-radius: var(--radius-control); }
.annotation-form { display: grid; gap: var(--space-4); }
.annotation-form label { display: grid; gap: var(--space-2); color: var(--muted); font-size: var(--text-xs); }
.annotation-form input, .annotation-form textarea { width: 100%; font-size: var(--text-sm); }
.annotation-form input { min-height: var(--control-height); }
.annotation-form .notice { margin: 0; }
.annotation-identity { font-size: var(--text-xs); color: var(--muted); overflow-wrap: anywhere; }
@media (max-width: 1399px) {
  .case-reading-grid { grid-template-columns: 1fr; }
}
@media (max-width: 760px) {
  .case-detail-body { gap: var(--space-5); }
  .case-score-picker { width: 100%; }
  .case-score-picker select { max-width: 100%; }
  .case-verdict { padding: var(--space-3); }
}
</style>

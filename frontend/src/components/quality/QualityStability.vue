<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, problem, validation } from '../../api'
import { extensions } from '../../api/extensions'
import type { ApiProblem, Run } from '../../types'
import type { RepeatBatch, Selection, StabilityAnalysis } from '../../types/extensions'
import { date, metricNames, number, pretty } from '../../workbench'
import AsyncState from '../AsyncState.vue'
import EvidenceNotice from '../EvidenceNotice.vue'
import ProblemBox from '../ProblemBox.vue'
import SectionCard from '../SectionCard.vue'
import StatusBadge from '../StatusBadge.vue'
import VersionPanel from '../VersionPanel.vue'

const props = defineProps<{ runId: string; scoreSetId: string }>()
const source = ref<Run | null>(null), sourceBusy = ref(false), sourceError = ref<ApiProblem | null>(null)
const repeatCount = ref(2), realConfirmed = ref(false), requestId = ref(crypto.randomUUID()), creating = ref(false), createError = ref<ApiProblem | null>(null), notice = ref('')
const batches = ref<RepeatBatch[]>([]), batchListBusy = ref(false), batchListError = ref<ApiProblem | null>(null), batchId = ref(''), batch = ref<RepeatBatch | null>(null), batchBusy = ref(false), batchError = ref<ApiProblem | null>(null)
const selections = ref<Selection[]>([]), analysis = ref<StabilityAnalysis | null>(null), analysisBusy = ref(false), analysisError = ref<ApiProblem | null>(null)
const asObject = (value: unknown) => value && typeof value === 'object' ? value as Record<string, unknown> : {}
const frozenBudget = computed(() => asObject(source.value?.snapshot.execution_budget))
const realExecution = computed(() => {
  if (!source.value) return false
  const agent = asObject(source.value.snapshot.agent), judge = asObject(source.value.snapshot.judge)
  return agent.source_type === 'REAL_EXTERNAL' || Boolean(judge.base_url && (judge.source_type || 'REAL_EXTERNAL') === 'REAL_EXTERNAL')
})
const perRunCalls = computed(() => typeof frozenBudget.value.max_total_calls === 'number' ? frozenBudget.value.max_total_calls : null)
const perRunSeconds = computed(() => {
  if (typeof frozenBudget.value.total_timeout_seconds === 'number') return frozenBudget.value.total_timeout_seconds
  return typeof frozenBudget.value.case_timeout_seconds === 'number' && source.value ? frozenBudget.value.case_timeout_seconds * source.value.total_cases : null
})
const realBudgetMissing = computed(() => realExecution.value && (perRunCalls.value === null || frozenBudget.value.total_timeout_seconds == null))
let sourceGeneration = 0, createGeneration = 0, listGeneration = 0, batchGeneration = 0, analysisGeneration = 0, timer: ReturnType<typeof setTimeout> | undefined, disposed = false
function stopPoll() { if (timer !== undefined) { clearTimeout(timer); timer = undefined } }
function prepareAnother() { requestId.value = crypto.randomUUID(); notice.value = '已准备新的请求号；下一次提交将创建新批次。' }
async function loadSource() {
  const generation = ++sourceGeneration
  source.value = null; sourceError.value = null; sourceBusy.value = Boolean(props.runId); realConfirmed.value = false; createError.value = null; notice.value = ''; creating.value = false; createGeneration++; requestId.value = crypto.randomUUID()
  if (!props.runId) return
  try { const result = await api.run(props.runId); if (!disposed && generation === sourceGeneration) source.value = result }
  catch (e) { if (!disposed && generation === sourceGeneration) sourceError.value = problem(e) }
  finally { if (!disposed && generation === sourceGeneration) sourceBusy.value = false }
}
async function loadBatches() {
  const generation = ++listGeneration
  batchListBusy.value = true; batchListError.value = null
  try { const result = await extensions.batches(); if (!disposed && generation === listGeneration) batches.value = result.items }
  catch (e) { if (!disposed && generation === listGeneration) batchListError.value = problem(e) }
  finally { if (!disposed && generation === listGeneration) batchListBusy.value = false }
}
async function pollBatch(id: string, generation: number) {
  batchBusy.value = true
  try {
    const result = await extensions.batch(id)
    if (disposed || generation !== batchGeneration) return
    batch.value = result; batchError.value = null
    batches.value = batches.value.map(item => item.id === result.id ? result : item)
    if (['QUEUED', 'RUNNING'].includes(result.status)) timer = setTimeout(() => { void pollBatch(id, generation) }, 2500)
  } catch (e) { if (!disposed && generation === batchGeneration) batchError.value = problem(e) }
  finally { if (!disposed && generation === batchGeneration) batchBusy.value = false }
}
function track(id: string) {
  stopPoll(); const generation = ++batchGeneration
  batchId.value = id; batch.value = null; batchError.value = null; batchBusy.value = false
  if (id) void pollBatch(id, generation)
}
async function createBatch() {
  if (!source.value || creating.value) return
  createError.value = validation([
    ...(source.value.status !== 'COMPLETED' ? [{ field: 'source_run_id', message: '来源运行尚未完成，请选择 COMPLETED 运行。' }] : []),
    ...(!Number.isInteger(repeatCount.value) || repeatCount.value < 2 || repeatCount.value > 5 ? [{ field: 'repeat_count', message: '新增运行数必须为 2—5。' }] : []),
    ...(realBudgetMissing.value ? [{ field: 'execution_budget', message: '来源缺少明确的调用与总时长预算，请选择预算完整的运行。' }] : []),
    ...(realExecution.value && !realConfirmed.value ? [{ field: 'confirm_real_execution', message: '请确认真实调用次数与合计预算。' }] : []),
  ])
  if (createError.value) return
  const generation = ++createGeneration
  creating.value = true; notice.value = ''
  try {
    const result = await extensions.repeat(source.value.id, repeatCount.value, realConfirmed.value, requestId.value)
    if (disposed || generation !== createGeneration) return
    notice.value = result.idempotent_replay ? '已找到同一请求创建的批次，继续查看持久进度。' : '重复批次已创建，按冻结配置顺序执行。'
    track(result.id); await loadBatches()
  } catch (e) { if (!disposed && generation === createGeneration) createError.value = problem(e) }
  finally { if (!disposed && generation === createGeneration) creating.value = false }
}
function resetAnalysis() { analysisGeneration++; analysis.value = null; analysisBusy.value = false; analysisError.value = null }
function addCurrent() {
  analysisError.value = validation([
    ...(!props.runId ? [{ field: 'selections', message: '先在上方选择运行。' }] : []),
    ...(selections.value.some(item => item.run_id === props.runId) ? [{ field: 'selections', message: '同一实际运行只能选一次，多个 RESCORE 不属于重复执行。' }] : []),
    ...(selections.value.length >= 10 ? [{ field: 'selections', message: '最多选择 10 条实际运行。' }] : []),
  ])
  if (analysisError.value) return
  resetAnalysis(); selections.value.push({ run_id: props.runId, score_set_id: props.scoreSetId || undefined })
}
function useBatch() {
  if (!batch.value) return
  resetAnalysis(); selections.value = batch.value.runs.slice(0, 10).map(item => ({ run_id: item.id, score_set_id: item.score_set_id }))
}
function removeSelection(index: number) { resetAnalysis(); selections.value.splice(index, 1) }
async function analyze() {
  if (analysisBusy.value) return
  analysisError.value = validation(selections.value.length < 2 ? [{ field: 'selections', message: '选择 2—10 条不同实际运行再分析。' }] : [])
  if (analysisError.value) return
  const generation = ++analysisGeneration
  analysisBusy.value = true; analysis.value = null
  try { const result = await extensions.stability(selections.value.map(item => ({ ...item }))); if (!disposed && generation === analysisGeneration) analysis.value = result }
  catch (e) { if (!disposed && generation === analysisGeneration) analysisError.value = problem(e) }
  finally { if (!disposed && generation === analysisGeneration) analysisBusy.value = false }
}
watch(() => props.runId, loadSource, { immediate: true })
watch([repeatCount, realConfirmed], () => { requestId.value = crypto.randomUUID(); createError.value = null })
onMounted(loadBatches)
onBeforeUnmount(() => { disposed = true; stopPoll(); sourceGeneration++; createGeneration++; listGeneration++; batchGeneration++; analysisGeneration++ })
</script>

<template>
  <div class="quality-stack">
    <SectionCard title="从冻结输入创建重复运行" description="新增 2—5 条独立运行，不含来源运行。每条沿用来源的 ORIGINAL 配置、用例与预算，共用单活动槽顺序执行。">
      <AsyncState :loading="sourceBusy" :error="sourceError" :empty="!runId" empty-title="先选择重复运行来源" empty-text="只有已完成运行可作为来源，历史回答不会被覆盖。" @retry="loadSource"><form v-if="source" class="quality-form" @submit.prevent="createBatch"><div class="quality-actions"><span>来源运行</span><StatusBadge :value="source.status" domain="run" /><span class="quality-id">{{ source.id }}</span></div><label>新增运行数<select v-model="repeatCount" :disabled="creating"><option v-for="value in [2,3,4,5]" :key="value" :value="value">{{ value }} 次独立新运行</option></select></label><div class="quality-facts"><span>每轮调用上限 <strong>{{ number(perRunCalls) }}</strong></span><span>合计调用上限 <strong>{{ number(perRunCalls === null ? null : perRunCalls * repeatCount) }}</strong></span><span>每轮时长上限 {{ number(perRunSeconds) }} 秒</span><span>合计时长上限 {{ number(perRunSeconds === null ? null : perRunSeconds * repeatCount) }} 秒</span></div><EvidenceNotice v-if="realExecution" title="这会重新调用真实 Agent 或 Judge" description="可能产生模型费用。只沿用来源已冻结的预算，不会自动扩大；合计上限按新增运行数计算。" tone="warning" /><p v-if="realBudgetMissing" class="small" style="color:var(--danger)">来源预算不完整，不能创建真实重复执行。请选择预算完整的运行。</p><label v-if="realExecution" class="quality-check"><input v-model="realConfirmed" type="checkbox" :disabled="creating" />我确认新增 {{ repeatCount }} 次真实执行，以及上面显示的合计调用与时长上限。</label><div v-if="notice" class="notice" role="status">{{ notice }}</div><ProblemBox :error="createError" /><div class="quality-actions"><ElButton native-type="submit" type="primary" :loading="creating" :disabled="source.status !== 'COMPLETED' || realBudgetMissing || (realExecution && !realConfirmed)">创建并启动重复批次</ElButton><ElButton :disabled="creating" @click="prepareAnother">准备另一批次</ElButton></div><p class="quality-id">请求号 {{ requestId }}。超时后先刷新批次核对；保留本号重试不会重复创建同一批次。</p><details class="raw-detail"><summary>查看来源冻结预算</summary><pre>{{ pretty(frozenBudget) }}</pre></details></form></AsyncState>
    </SectionCard>
    <SectionCard title="重复批次与持久进度" description="活动批次每 2.5 秒读取进度。中断后保留已执行记录，未执行成员不会自动重放。"><template #actions><ElButton :loading="batchListBusy" @click="loadBatches">刷新批次列表</ElButton></template><AsyncState :loading="batchListBusy" :error="batchListError" :empty="!batches.length" empty-title="尚无重复批次" empty-text="可在上方创建批次，或在下方直接选择已有运行做稳定性分析。" @retry="loadBatches"><label class="quality-field">选择批次<select :value="batchId" @change="track(($event.target as HTMLSelectElement).value)"><option value="">选择批次查看进度</option><option v-for="item in batches" :key="item.id" :value="item.id">{{ date(item.created_at) }} · {{ item.status }} · {{ item.repeat_count }} 次 · {{ item.id.slice(0,8) }}</option></select></label></AsyncState><ProblemBox :error="batchError" /><div v-if="batchId" class="quality-actions" style="margin-top:var(--space-4)"><ElButton :loading="batchBusy" @click="track(batchId)">刷新当前批次</ElButton></div><div v-if="batch" class="quality-stack" style="margin-top:var(--space-4)"><div class="quality-record-title"><span class="quality-id">{{ batch.id }}</span><StatusBadge :value="batch.status" domain="run" /></div><p class="small">合计调用上限 {{ number(batch.budget.total_max_calls) }} · 合计时长上限 {{ number(batch.budget.total_timeout_seconds) }} 秒 · {{ batch.budget.real_execution ? '含真实执行' : '非真实调用来源' }}</p><div v-if="batch.error" class="quality-summary quality-summary--warning"><p>批次未正常完成。保留已执行记录，请检查错误和各运行证据。</p><pre>{{ pretty(batch.error) }}</pre></div><div class="table-scroll"><table><thead><tr><th>顺序</th><th>运行</th><th>执行状态</th><th>证据</th></tr></thead><tbody><tr v-for="item in batch.runs" :key="item.id"><td>{{ item.ordinal }}</td><td class="mono">{{ item.id }}</td><td><StatusBadge :value="item.status" domain="run" /></td><td><RouterLink class="text-link" :to="{ path: '/reports', query: { run_id: item.id, score_set_id: item.score_set_id } }">报告 →</RouterLink></td></tr></tbody></table></div><div><ElButton :disabled="batch.runs.length < 2" @click="useBatch">将这些运行加入稳定性分析</ElButton></div></div></SectionCard>
    <SectionCard title="分析已选运行" description="选择 2—10 条不同实际运行。多个重评分不能代替重复执行；未知值不补零，不同冻结条件不合并统计。"><div class="quality-stack"><div class="quality-actions"><ElButton :disabled="!runId" @click="addCurrent">加入上方所选运行与评分集</ElButton><span class="small muted">已选 {{ selections.length }} / 10</span></div><div v-if="!selections.length" class="small muted">选择上方运行并加入，或从批次加入独立运行。</div><div v-for="(item,index) in selections" :key="item.run_id" class="quality-record"><div class="quality-record-title"><span class="quality-id">运行 {{ item.run_id }}<br />评分集 {{ item.score_set_id || 'ORIGINAL（后端解析固定原评分）' }}</span><ElButton @click="removeSelection(index)">移除</ElButton></div></div><ProblemBox :error="analysisError" /><div><ElButton type="primary" :loading="analysisBusy" @click="analyze">分析已保存运行</ElButton></div></div></SectionCard>
    <SectionCard v-if="analysis" title="稳定性分析结果"><div class="quality-stack"><div class="quality-summary" :class="{ 'quality-summary--warning': !analysis.comparable }"><strong>{{ analysis.comparable ? '冻结条件一致，可统计已知观测' : '冻结条件不一致，统计不可合并' }}</strong><p class="small">{{ analysis.run_count }} 条实际运行；结论翻转与分数波动仅描述这些已保存观测。</p></div><div class="quality-facts"><span>冻结条件一致：{{ analysis.conditions.frozen_conditions_match ? '是' : '否' }}</span><span>全部完成：{{ analysis.conditions.completed ? '是' : '否' }}</span><span>实际版本均已核验：{{ analysis.conditions.actual_versions_verified ? '是' : '否' }}</span></div><ul v-if="analysis.limitations.length" class="limitation-list"><li v-for="item in analysis.limitations" :key="item">{{ item }}</li></ul><section><h3>逐题 P / F / U 与结论翻转</h3><div class="table-scroll"><table><thead><tr><th>用例</th><th>P / F / U</th><th>已知 / 总观测</th><th>已知结论翻转</th><th>各次结果与证据</th></tr></thead><tbody><tr v-for="item in analysis.cases" :key="item.case_key"><td class="mono">{{ item.case_key }}</td><td>{{ number(item.P) }} / {{ number(item.F) }} / {{ number(item.U) }}</td><td>{{ item.valid_count }} / {{ item.total_count }}</td><td>{{ item.flipped === null ? '不可用' : item.flipped ? '有翻转' : '未见翻转' }} · {{ number(item.flip_count) }}</td><td><div class="quality-actions"><RouterLink v-for="observation in item.observations" :key="observation.run_id" class="text-link small" :to="{ path: '/evaluations', query: { run_id: observation.run_id, score_set_id: observation.score_set_id, case_run_id: observation.case_run_id } }">{{ observation.run_id.slice(0,8) }} · {{ observation.conclusion || '未判定' }} · {{ observation.agent_status || '未知执行' }}</RouterLink></div></td></tr></tbody></table></div></section><section><h3>指标波动</h3><p class="small muted">均值与范围基于全部有效逐题执行观测；标准差采用总体口径，单个有效值不报告标准差。</p><div class="table-scroll"><table><thead><tr><th>指标</th><th>有效 / 总观测</th><th>均值</th><th>标准差</th><th>范围</th><th>限制</th></tr></thead><tbody><tr v-for="item in analysis.metrics" :key="item.metric"><td>{{ metricNames[item.metric] || item.metric }}</td><td>{{ item.valid_count }} / {{ item.total_count }}</td><td>{{ number(item.mean) }}</td><td>{{ number(item.stddev) }}</td><td>{{ number(item.min) }} — {{ number(item.max) }}</td><td>{{ item.reason || '—' }}</td></tr></tbody></table></div></section><details v-for="item in analysis.runs" :key="item.run_id" class="raw-detail"><summary>{{ item.run_id.slice(0,8) }} · {{ item.kind }} · {{ item.run_status }} / {{ item.score_set_status }} · 实际版本 {{ item.version_verification.status }}</summary><VersionPanel :value="item.version_verification" /><pre>{{ pretty({ source_scope: item.source_scope, summary: item.summary }) }}</pre></details></div></SectionCard>
  </div>
</template>

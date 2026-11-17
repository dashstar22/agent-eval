<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { api, problem, validation } from '../../api'
import { extensions } from '../../api/extensions'
import type { ApiProblem, Dataset } from '../../types'
import type { RegressionDraft, ReviewCase } from '../../types/extensions'
import { allPages, date, pretty } from '../../workbench'
import AsyncState from '../AsyncState.vue'
import EvidenceNotice from '../EvidenceNotice.vue'
import ProblemBox from '../ProblemBox.vue'
import SectionCard from '../SectionCard.vue'
import StatusBadge from '../StatusBadge.vue'

const props = defineProps<{ runId: string; scoreSetId: string; caseRunId: string }>()
const emit = defineEmits<{ selectCase: [id: string] }>()
const datasets = ref<Dataset[]>([]), choices = ref<{ id: string; case_key: string; question: string }[]>([]), choicesLoading = ref(false), choicesError = ref<ApiProblem | null>(null)
const source = ref<ReviewCase | null>(null), sourceBusy = ref(false), sourceError = ref<ApiProblem | null>(null), targetDataset = ref(0), caseKey = ref(''), expectedAnswer = ref(''), reason = ref('')
const formError = ref<ApiProblem | null>(null), saving = ref(false), notice = ref('')
const drafts = ref<RegressionDraft[]>([]), filterDataset = ref(0), draftsBusy = ref(false), draftsError = ref<ApiProblem | null>(null)
const reviewing = ref<RegressionDraft | null>(null), reviewer = ref(''), confirmed = ref(false), approving = ref(false), approvalError = ref<ApiProblem | null>(null)
let choiceGeneration = 0, sourceGeneration = 0, listGeneration = 0, approvalGeneration = 0, disposed = false
async function loadChoices() {
  const generation = ++choiceGeneration
  choices.value = []; choicesError.value = null; choicesLoading.value = true
  try {
    const [available, cases] = await Promise.all([allPages(page => api.datasets(page)), props.runId ? extensions.reviewCases(props.runId, props.scoreSetId) : Promise.resolve({ items: [] })])
    if (!disposed && generation === choiceGeneration) { datasets.value = available; choices.value = cases.items }
  } catch (e) { if (!disposed && generation === choiceGeneration) choicesError.value = problem(e) }
  finally { if (!disposed && generation === choiceGeneration) choicesLoading.value = false }
}
async function loadSource() {
  const generation = ++sourceGeneration
  source.value = null; sourceError.value = null; formError.value = null; sourceBusy.value = Boolean(props.caseRunId && props.runId); saving.value = false
  caseKey.value = ''; expectedAnswer.value = ''; reason.value = ''; notice.value = ''
  if (!props.caseRunId || !props.runId) return
  try {
    const result = await extensions.reviewCase(props.caseRunId, props.scoreSetId, true)
    if (disposed || generation !== sourceGeneration) return
    if (result.case.run_id !== props.runId) throw new Error('该用例不属于当前来源运行。')
    source.value = result; caseKey.value = `${result.case.case_key.slice(0, 80)}.regression`; expectedAnswer.value = result.case.expected_answer
  } catch (e) { if (!disposed && generation === sourceGeneration) sourceError.value = problem(e) }
  finally { if (!disposed && generation === sourceGeneration) sourceBusy.value = false }
}
async function loadDrafts() {
  const generation = ++listGeneration
  draftsBusy.value = true; draftsError.value = null; drafts.value = []
  try { const result = await extensions.drafts(filterDataset.value || undefined); if (!disposed && generation === listGeneration) drafts.value = result.items }
  catch (e) { if (!disposed && generation === listGeneration) draftsError.value = problem(e) }
  finally { if (!disposed && generation === listGeneration) draftsBusy.value = false }
}
async function create() {
  if (!source.value || saving.value) return
  formError.value = validation([
    ...(!targetDataset.value ? [{ field: 'target_dataset_id', message: '选择候选的目标数据集。' }] : []),
    ...(!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/.test(caseKey.value) ? [{ field: 'case_key', message: '用例标识需为 1—100 位字母、数字、点、下划线或短横线。' }] : []),
    ...(!expectedAnswer.value.trim() ? [{ field: 'expected_answer', message: '填写人工整理的预期行为。' }] : []),
    ...(!reason.value.trim() ? [{ field: 'reason', message: '说明加入回归集的原因。' }] : []),
  ])
  if (formError.value) return
  const generation = sourceGeneration, target = source.value
  saving.value = true; notice.value = ''
  try {
    const result = await extensions.createDraft({ case_run_id: target.case.id, score_set_id: target.score_set.id, target_dataset_id: targetDataset.value, case_key: caseKey.value, expected_answer: expectedAnswer.value.trim(), reason: reason.value.trim() })
    if (disposed || generation !== sourceGeneration) return
    notice.value = `候选 ${result.case_key} 已保存为待审核，不会自动启用。`; filterDataset.value = result.target_dataset_id; await loadDrafts()
  } catch (e) { if (!disposed && generation === sourceGeneration) formError.value = problem(e) }
  finally { if (!disposed && generation === sourceGeneration) saving.value = false }
}
function inspect(draft: RegressionDraft) { reviewing.value = draft; reviewer.value = ''; confirmed.value = false; approvalError.value = null; approving.value = false; approvalGeneration++ }
async function approve() {
  if (!reviewing.value || approving.value) return
  approvalError.value = validation([
    ...(!reviewer.value.trim() ? [{ field: 'reviewer_name', message: '填写审核人标识。' }] : []),
    ...(!confirmed.value ? [{ field: 'human_confirmed', message: '核对冻结问题、预期行为与来源后勾选确认。' }] : []),
  ])
  if (approvalError.value) return
  const generation = ++approvalGeneration, target = reviewing.value
  approving.value = true
  try {
    const result = await extensions.approveDraft(target.id, reviewer.value.trim())
    if (disposed || generation !== approvalGeneration) return
    reviewing.value = result; confirmed.value = false; notice.value = `审核完成：${result.case_key} 已加入数据集，当前为禁用状态。请到数据集检查后启用。`; await loadDrafts()
  } catch (e) { if (!disposed && generation === approvalGeneration) approvalError.value = problem(e) }
  finally { if (!disposed && generation === approvalGeneration) approving.value = false }
}
watch(() => [props.runId, props.scoreSetId], loadChoices, { immediate: true })
watch(() => [props.runId, props.scoreSetId, props.caseRunId], loadSource, { immediate: true })
watch(filterDataset, loadDrafts, { immediate: true })
onBeforeUnmount(() => { disposed = true; choiceGeneration++; sourceGeneration++; listGeneration++; approvalGeneration++ })
</script>

<template>
  <div class="quality-stack">
    <div v-if="notice" class="notice" role="status">{{ notice }}</div>
    <SectionCard title="把失败证据转为回归候选" description="复制冻结问题、工具预期与前置条件；人工整理预期行为，再审核进入数据集。">
      <AsyncState :loading="choicesLoading" :error="choicesError" @retry="loadChoices"><label class="quality-field">来源用例<select :value="caseRunId" :disabled="!runId || saving" @change="emit('selectCase', ($event.target as HTMLSelectElement).value)"><option value="">{{ runId ? '选择来源用例' : '先选择来源运行' }}</option><option v-if="caseRunId && !choices.some(item => item.id === caseRunId)" :value="caseRunId">深链用例 · {{ caseRunId }}</option><option v-for="item in choices" :key="item.id" :value="item.id">{{ item.case_key }} · {{ item.question.slice(0, 90) }}</option></select></label></AsyncState>
      <AsyncState v-if="runId && caseRunId" :loading="sourceBusy" :error="sourceError" @retry="loadSource"><form v-if="source" class="quality-form" style="margin-top:var(--space-5)" @submit.prevent="create"><EvidenceNotice title="参考行为由人工检查" description="预填原冻结参考行为。失败回答不会自动成为标准答案；审核后创建禁用用例，不会改动历史运行。" tone="warning" /><div><h3>冻结问题</h3><p class="preserve-text">{{ source.case.question }}</p></div><div class="quality-fields"><label>目标数据集<select v-model="targetDataset" :disabled="saving"><option :value="0">请选择数据集</option><option v-for="item in datasets" :key="item.id" :value="item.id">{{ item.name }}</option></select></label><label>候选用例标识<input v-model="caseKey" maxlength="100" :disabled="saving" /></label></div><label>人工整理的预期行为<textarea v-model="expectedAnswer" rows="4" maxlength="20000" :disabled="saving" /></label><label>加入回归集的原因<textarea v-model="reason" rows="3" maxlength="20000" :disabled="saving" /></label><details class="raw-detail"><summary>核对工具断言、前置条件与来源</summary><pre>{{ pretty({ expected_tools: source.case.expected_tools, tool_match_mode: source.case.tool_match_mode, tool_assertions: source.case.tool_assertions, preconditions: source.case.preconditions, run_id: source.case.run_id, case_run_id: source.case.id, score_set_id: source.score_set.id }) }}</pre></details><ProblemBox :error="formError" /><div><ElButton native-type="submit" type="primary" :loading="saving">保存待审候选</ElButton></div></form></AsyncState>
      <p v-else class="small muted" style="margin-top:var(--space-4)">从评测详情的“转为回归候选”进入，或在此选择来源运行与用例。</p>
    </SectionCard>
    <SectionCard title="待审与已审核候选" description="同名冲突不会覆盖已有用例；已审核候选保留来源与审核记录。"><template #actions><ElButton :loading="draftsBusy" @click="loadDrafts">刷新候选</ElButton></template><label class="quality-field">按目标数据集筛选<select v-model="filterDataset"><option :value="0">全部数据集</option><option v-for="item in datasets" :key="item.id" :value="item.id">{{ item.name }}</option></select></label><AsyncState :loading="draftsBusy" :error="draftsError" :empty="!drafts.length" empty-title="暂无回归候选" empty-text="选择已保存用例，补充预期行为与理由后创建。" @retry="loadDrafts"><div class="quality-list" style="margin-top:var(--space-4)"><article v-for="draft in drafts" :key="draft.id" class="quality-record"><div class="quality-record-title"><strong class="mono">{{ draft.case_key }}</strong><StatusBadge :value="draft.status" /></div><p class="small muted">目标数据集 {{ datasets.find(item => item.id === draft.target_dataset_id)?.name || draft.target_dataset_id }} · {{ date(draft.created_at) }}</p><p class="preserve-text">{{ draft.reason }}</p><div class="quality-actions"><ElButton @click="inspect(draft)">{{ draft.status === 'PENDING' ? '审核此候选' : '查看审核记录' }}</ElButton><RouterLink class="text-link small" :to="{ path: '/evaluations', query: { run_id: draft.run_id, score_set_id: draft.score_set_id, case_run_id: draft.case_run_id } }">原始证据 →</RouterLink><RouterLink v-if="draft.status === 'APPROVED'" class="text-link small" :to="{ path: '/datasets', query: { dataset: draft.target_dataset_id } }">到数据集检查并启用 →</RouterLink></div></article></div></AsyncState></SectionCard>
    <SectionCard v-if="reviewing" :title="`审核候选 · ${reviewing.case_key}`"><div class="quality-stack"><div class="quality-reading"><section><h3>问题</h3><p class="preserve-text">{{ reviewing.case_snapshot.question }}</p></section><section><h3>预期行为</h3><p class="preserve-text">{{ reviewing.case_snapshot.expected_answer }}</p></section></div><p class="preserve-text">理由：{{ reviewing.reason }}</p><details class="raw-detail" open><summary>完整候选快照与来源</summary><pre>{{ pretty({ case_snapshot: reviewing.case_snapshot, run_id: reviewing.run_id, case_run_id: reviewing.case_run_id, score_set_id: reviewing.score_set_id, target_dataset_id: reviewing.target_dataset_id }) }}</pre></details><form v-if="reviewing.status === 'PENDING'" class="quality-form" @submit.prevent="approve"><label>审核人标识<input v-model="reviewer" maxlength="100" :disabled="approving" /></label><label class="quality-check"><input v-model="confirmed" type="checkbox" :disabled="approving" />我已人工核对问题、预期行为与来源，确认创建禁用回归用例。</label><ProblemBox :error="approvalError" /><div><ElButton native-type="submit" type="primary" :loading="approving">审核并创建禁用用例</ElButton></div></form><div v-else class="quality-summary quality-summary--pass"><p>已由 {{ reviewing.approved_by }} 于 {{ date(reviewing.approved_at) }} 审核。创建用例 ID：{{ reviewing.test_case_id }}。</p><RouterLink class="text-link" :to="{ path: '/datasets', query: { dataset: reviewing.target_dataset_id } }">前往数据集 →</RouterLink></div></div></SectionCard>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, problem, validation } from '../api'
import type { ApiProblem, CaseInput, Dataset, TestCase } from '../types'
import AsyncState from '../components/AsyncState.vue'
import ProblemBox from '../components/ProblemBox.vue'
import FormModal from '../components/FormModal.vue'

const route = useRoute(), router = useRouter()
const datasets = ref<Dataset[]>([]), total = ref(0), page = ref(1), loading = ref(true), loadError = ref<ApiProblem | null>(null)
const selected = ref<Dataset | null>(null), cases = ref<TestCase[]>([]), caseTotal = ref(0), casePage = ref(1), caseLoading = ref(false), caseError = ref<ApiProblem | null>(null)
const filterEnabled = ref('all'), filterTag = ref(''), notice = ref(''), actionError = ref<ApiProblem | null>(null), busy = ref(false)
const datasetModal = ref(false), datasetId = ref<number>(), datasetForm = reactive({ name: '', description: '' })
const caseModal = ref(false), caseKey = ref<string>(), saving = ref(false), formError = ref<ApiProblem | null>(null)
const metrics = [
  ['execution_completed', '执行完成'], ['answer_nonempty', '回答非空'], ['correctness', '正确性'], ['relevance', '相关性'], ['task_completion', '任务完成度'],
  ['faithfulness', '完整上下文支持'], ['expected_tools', '预期工具'], ['tool_success', '工具成功'], ['response_time', '响应时延'], ['tool_names', '工具名称合法'],
]
const defaults = ['execution_completed', 'answer_nonempty', 'correctness', 'relevance', 'task_completion']
const caseForm = reactive({ case_key: '', question: '', expected_answer: '', toolsMode: 'skip', tools: '', required_metrics: [...defaults], tags: '', enabled: true, device_ref: '', order_id: '', prior_turns: '', source: '{}', license: 'UNSPECIFIED', seen_regression: false })
let datasetGeneration = 0, caseGeneration = 0
const selectedId = computed(() => { const value = Number(route.query.dataset); return Number.isInteger(value) && value > 0 ? value : null })
async function loadDatasets() {
  const generation = ++datasetGeneration
  loading.value = true; loadError.value = null
  try { const data = await api.datasets(page.value); if (generation === datasetGeneration) { datasets.value = data.items; total.value = data.total } }
  catch (error) { if (generation === datasetGeneration) loadError.value = problem(error) }
  finally { if (generation === datasetGeneration) loading.value = false }
}
async function loadCases() {
  const generation = ++caseGeneration, id = selectedId.value
  caseError.value = null
  if (!id) { selected.value = null; cases.value = []; caseLoading.value = false; return }
  caseLoading.value = true
  try {
    const [dataset, data] = await Promise.all([api.dataset(id), api.cases(id, casePage.value, filterEnabled.value === 'all' ? undefined : filterEnabled.value === 'enabled', filterTag.value.trim())])
    if (generation === caseGeneration) { selected.value = dataset; cases.value = data.items; caseTotal.value = data.total }
  } catch (error) { if (generation === caseGeneration) { caseError.value = problem(error); selected.value = null } }
  finally { if (generation === caseGeneration) caseLoading.value = false }
}
watch(selectedId, () => { casePage.value = 1; filterEnabled.value = 'all'; filterTag.value = ''; void loadCases() })
onMounted(() => { void loadDatasets(); void loadCases() })
function selectDataset(id: number) { void router.replace({ query: { dataset: id } }) }
function editDataset(row?: Dataset) { formError.value = null; notice.value = ''; datasetId.value = row?.id; Object.assign(datasetForm, { name: row?.name || '', description: row?.description || '' }); datasetModal.value = true }
async function saveDataset() {
  if (saving.value) return
  formError.value = validation(!datasetForm.name.trim() ? [{ field: 'name', message: '请输入数据集名称。' }] : [])
  if (formError.value) return
  saving.value = true
  try { const row = await api.saveDataset({ name: datasetForm.name.trim(), description: datasetForm.description }, datasetId.value); datasetModal.value = false; notice.value = '数据集已保存'; await loadDatasets(); if (selectedId.value === row.id) await loadCases(); else selectDataset(row.id) }
  catch (error) { formError.value = problem(error) }
  finally { saving.value = false }
}
async function deleteDataset(row: Dataset) {
  if (!window.confirm(`删除数据集“${row.name}”？后续运行将不能选用它，历史冻结记录仍会保留。`)) return
  busy.value = true; actionError.value = null; notice.value = ''
  try { await api.deleteDataset(row.id); if (selectedId.value === row.id) await router.replace({ query: {} }); notice.value = '数据集已删除'; if (datasets.value.length === 1 && page.value > 1) page.value--; await loadDatasets() }
  catch (error) { actionError.value = problem(error) }
  finally { busy.value = false }
}
function openCase(row?: TestCase) {
  formError.value = null; notice.value = ''; caseKey.value = row?.case_key
  Object.assign(caseForm, { case_key: row?.case_key || '', question: row?.question || '', expected_answer: row?.expected_answer || '', toolsMode: row?.expected_tools == null ? 'skip' : row.tool_match_mode, tools: row?.expected_tools?.join(', ') || '', required_metrics: [...(row?.required_metrics || defaults)], tags: row?.tags.join(', ') || '', enabled: row?.enabled ?? true, device_ref: row?.preconditions.device_ref || '', order_id: row?.preconditions.order_id?.toString() || '', prior_turns: row?.preconditions.prior_turns.join('\n') || '', source: JSON.stringify(row?.source || {}, null, 2), license: row?.license || 'UNSPECIFIED', seen_regression: row?.seen_regression ?? false })
  caseModal.value = true
}
const split = (value: string) => value.split(/[,，\n]/).map(item => item.trim()).filter(Boolean)
async function saveCase() {
  if (saving.value) return
  const errors: { field: string; message: string }[] = []
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/.test(caseForm.case_key)) errors.push({ field: 'case_key', message: '使用 1—100 个字母、数字、点、下划线或短横线，首位为字母或数字。' })
  if (!caseForm.question.trim()) errors.push({ field: 'question', message: '请输入需要发送给 Agent 的问题。' })
  if (!caseForm.expected_answer.trim()) errors.push({ field: 'expected_answer', message: '请输入参考答案或预期行为。' })
  if (!caseForm.required_metrics.length) errors.push({ field: 'required_metrics', message: '至少选择一个必评指标。' })
  const tools = caseForm.toolsMode === 'skip' ? null : split(caseForm.tools), tags = split(caseForm.tags)
  if (caseForm.toolsMode === 'contains' && !tools?.length) errors.push({ field: 'expected_tools', message: '“至少包含”需要填写工具；不检查请选择“不检查”。' })
  if (tools && new Set(tools).size !== tools.length) errors.push({ field: 'expected_tools', message: '工具名称不能重复。' })
  if (new Set(tags).size !== tags.length) errors.push({ field: 'tags', message: '标签不能重复。' })
  if (!caseForm.license.trim()) errors.push({ field: 'license', message: '请输入许可；尚不明确可保留 UNSPECIFIED。' })
  const orderId = caseForm.order_id.trim() ? Number(caseForm.order_id) : null
  if (orderId !== null && (!Number.isSafeInteger(orderId) || orderId <= 0)) errors.push({ field: 'preconditions.order_id', message: '订单 ID 必须为正整数或留空。' })
  let source: Record<string, unknown> = {}
  try { const value: unknown = JSON.parse(caseForm.source); if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(); source = value as Record<string, unknown> }
  catch { errors.push({ field: 'source', message: '来源说明必须为合法 JSON 对象，例如 {"kind":"SYNTHETIC"}。' }) }
  const prior = caseForm.prior_turns.split('\n').map(item => item.trim()).filter(Boolean)
  if (prior.length > 19) errors.push({ field: 'preconditions.prior_turns', message: '前置轮次最多 19 条，每行一条。' })
  formError.value = validation(errors)
  if (formError.value || !selectedId.value) return
  const input: CaseInput = { case_key: caseForm.case_key, question: caseForm.question.trim(), expected_answer: caseForm.expected_answer.trim(), expected_tools: tools, tool_match_mode: caseForm.toolsMode === 'exact' ? 'exact' : 'contains', required_metrics: caseForm.required_metrics, tags, enabled: caseForm.enabled, preconditions: { device_ref: caseForm.device_ref.trim() || null, order_id: orderId, prior_turns: prior }, source, license: caseForm.license.trim(), seen_regression: caseForm.seen_regression }
  saving.value = true
  try { await api.saveCase(selectedId.value, input, caseKey.value); caseModal.value = false; notice.value = '用例已保存'; await Promise.all([loadCases(), loadDatasets()]) }
  catch (error) { formError.value = problem(error) }
  finally { saving.value = false }
}
async function toggleCase(row: TestCase) {
  if (!selectedId.value) return
  busy.value = true; actionError.value = null; notice.value = ''
  try { await api.toggleCase(selectedId.value, row.case_key, !row.enabled); notice.value = row.enabled ? '用例已停用' : '用例已启用'; await Promise.all([loadCases(), loadDatasets()]) }
  catch (error) { actionError.value = problem(error) }
  finally { busy.value = false }
}
async function deleteCase(row: TestCase) {
  if (!selectedId.value || !window.confirm(`删除用例“${row.case_key}”？稳定标识不再复用，历史冻结内容仍保留。`)) return
  busy.value = true; actionError.value = null; notice.value = ''
  try { await api.deleteCase(selectedId.value, row.case_key); if (cases.value.length === 1 && casePage.value > 1) casePage.value--; notice.value = '用例已删除'; await Promise.all([loadCases(), loadDatasets()]) }
  catch (error) { actionError.value = problem(error) }
  finally { busy.value = false }
}
function applyFilter() { casePage.value = 1; void loadCases() }
</script>

<template>
  <div class="page-heading"><div><div class="eyebrow">EVALUATION INPUTS</div><h1>数据集</h1><p>维护问题、预期行为和执行前置条件，为下一次运行准备输入。</p></div><ElButton type="primary" @click="editDataset()">＋ 新建数据集</ElButton></div>
  <div v-if="notice" class="notice" role="status">✓ {{ notice }}</div><ProblemBox :error="actionError" />
  <section class="panel"><div class="panel-heading"><h2>已保存的数据集 <span class="count-label">{{ total }}</span></h2><ElButton :disabled="loading" @click="loadDatasets">刷新数据集</ElButton></div>
    <AsyncState :loading="loading" :error="loadError" :empty="!datasets.length" empty-title="还没有数据集" empty-text="新建数据集后，添加问题和参考行为。" @retry="loadDatasets">
      <template #empty-action><ElButton type="primary" @click="editDataset()">新建数据集</ElButton></template>
      <div class="table-scroll"><table><thead><tr><th>名称 / 说明</th><th>启用用例</th><th>数据版本</th><th class="actions-column">操作</th></tr></thead><tbody><tr v-for="row in datasets" :key="row.id" :class="{ selected: selectedId === row.id }"><td><button class="text-link record-title" :aria-label="`查看数据集 ${row.name}`" @click="selectDataset(row.id)">{{ row.name }}</button><div class="table-subtext">{{ row.description || '暂无说明' }}</div></td><td><span class="number">{{ row.enabled_cases }}</span><span class="muted"> 条</span></td><td><span class="revision">r{{ row.revision }}</span></td><td><div class="row-actions"><ElButton size="small" :aria-label="`编辑数据集 ${row.name}`" :disabled="busy" @click="editDataset(row)">编辑</ElButton><ElButton size="small" type="danger" plain :aria-label="`删除数据集 ${row.name}`" :disabled="busy" @click="deleteDataset(row)">删除</ElButton></div></td></tr></tbody></table></div>
      <div v-if="total > 20" class="pagination"><ElPagination v-model:current-page="page" :page-size="20" :total="total" layout="prev, pager, next" @current-change="loadDatasets" /></div>
    </AsyncState>
  </section>
  <section v-if="selectedId" class="panel case-panel"><div class="panel-heading"><div><div class="eyebrow">TEST CASES</div><h2>{{ selected?.name || '数据集用例' }}</h2><p v-if="selected" class="muted small">{{ selected.enabled_cases }} 条启用 · 新运行只冻结启用的用例</p></div><ElButton type="primary" :disabled="!selected || caseLoading" @click="openCase()">＋ 新建用例</ElButton></div>
    <form class="filter-row" @submit.prevent="applyFilter"><label>启用状态<select v-model="filterEnabled" aria-label="筛选启用状态"><option value="all">全部状态</option><option value="enabled">已启用</option><option value="disabled">已停用</option></select></label><label>标签<input v-model="filterTag" aria-label="筛选标签" placeholder="精确匹配标签" /></label><ElButton native-type="submit" :disabled="caseLoading">筛选用例</ElButton><span class="muted small">{{ caseTotal }} 条匹配用例</span></form>
    <AsyncState :loading="caseLoading" :error="caseError" :empty="!cases.length" empty-title="没有匹配的用例" empty-text="添加用例，或调整启用状态与标签筛选。" @retry="loadCases">
      <div class="table-scroll"><table><thead><tr><th>标识 / 问题</th><th>标签</th><th>工具预期</th><th>状态</th><th class="actions-column">操作</th></tr></thead><tbody><tr v-for="row in cases" :key="row.id"><td class="case-question"><button class="text-link mono" :aria-label="`查看用例 ${row.case_key}`" @click="openCase(row)">{{ row.case_key }}</button><div>{{ row.question }}</div><details class="inline-detail"><summary>参考行为与来源</summary><p>{{ row.expected_answer }}</p><pre>{{ JSON.stringify(row.source, null, 2) }}</pre><span class="small muted">{{ row.license }} · {{ row.seen_regression ? '已见回归题' : '未标为已见回归题' }}</span></details></td><td><div class="tag-list"><ElTag v-for="tag in row.tags" :key="tag" size="small" effect="plain">{{ tag }}</ElTag><span v-if="!row.tags.length" class="muted">—</span></div></td><td class="small">{{ row.expected_tools === null ? '不检查' : row.tool_match_mode === 'exact' ? '仅允许' : '至少包含' }}<div class="mono muted">{{ row.expected_tools === null ? 'null' : row.expected_tools.length ? row.expected_tools.join(', ') : '无工具调用' }}</div></td><td><ElTag :type="row.enabled ? 'success' : 'info'" size="small">{{ row.enabled ? '已启用' : '已停用' }}</ElTag></td><td><div class="row-actions"><ElButton size="small" :aria-label="`编辑用例 ${row.case_key}`" :disabled="busy" @click="openCase(row)">编辑</ElButton><ElButton size="small" :aria-label="`${row.enabled ? '停用' : '启用'}用例 ${row.case_key}`" :disabled="busy" @click="toggleCase(row)">{{ row.enabled ? '停用' : '启用' }}</ElButton><ElButton size="small" type="danger" plain :aria-label="`删除用例 ${row.case_key}`" :disabled="busy" @click="deleteCase(row)">删除</ElButton></div></td></tr></tbody></table></div>
      <div v-if="caseTotal > 20" class="pagination"><ElPagination v-model:current-page="casePage" :page-size="20" :total="caseTotal" layout="prev, pager, next" @current-change="loadCases" /></div>
    </AsyncState>
  </section>
  <div v-else class="selection-hint">选择一个数据集，查看和维护其中的用例。</div>

  <FormModal v-if="datasetModal" :title="datasetId ? '编辑数据集' : '新建数据集'" :saving="saving" @close="!saving && (datasetModal = false)"><form class="editor-form" novalidate @submit.prevent="saveDataset"><ProblemBox :error="formError" /><label>数据集名称 <span class="required">*</span><input v-model="datasetForm.name" aria-label="数据集名称" maxlength="200" placeholder="例如：打印机基础问答回归" /></label><label>说明<textarea v-model="datasetForm.description" aria-label="数据集说明" rows="3" maxlength="10000" placeholder="记录适用范围、数据来源和用途" /></label><div class="form-actions"><ElButton :disabled="saving" @click="datasetModal = false">取消</ElButton><ElButton type="primary" native-type="submit" :loading="saving">保存数据集</ElButton></div></form></FormModal>

  <FormModal v-if="caseModal" :title="caseKey ? '编辑用例' : '新建用例'" :saving="saving" wide @close="!saving && (caseModal = false)"><form class="editor-form" novalidate @submit.prevent="saveCase"><ProblemBox :error="formError" /><div class="form-grid"><label>稳定用例标识 <span class="required">*</span><input v-model="caseForm.case_key" aria-label="稳定用例标识" :disabled="!!caseKey" maxlength="100" placeholder="PRINTER-001" /><small>保存后保持不变，删除后也不复用。</small></label><label class="check-field"><input v-model="caseForm.enabled" type="checkbox" aria-label="启用用例" /> 启用此用例<small>仅影响后续新运行。</small></label></div><label>问题 <span class="required">*</span><textarea v-model="caseForm.question" aria-label="问题" rows="3" maxlength="20000" placeholder="实际发送给被测 Agent 的用户问题" /></label><label>参考答案或预期行为 <span class="required">*</span><textarea v-model="caseForm.expected_answer" aria-label="参考答案或预期行为" rows="3" maxlength="20000" placeholder="说明应该回答、拒答或追问什么；此内容不会发给被测 Agent" /></label><div class="form-grid"><label>预期工具匹配<select v-model="caseForm.toolsMode" aria-label="预期工具匹配"><option value="skip">不检查工具（null）</option><option value="contains">至少包含这些工具</option><option value="exact">仅允许这些工具（可为空）</option></select></label><label>工具名称<input v-model="caseForm.tools" aria-label="工具名称" :disabled="caseForm.toolsMode === 'skip'" placeholder="逗号分隔，例如 searchKnowledge" /><small>{{ caseForm.toolsMode === 'exact' ? '留空表示禁止调用工具（空集合）。' : '工具名区分大小写，不能重复。' }}</small></label></div><label>标签<input v-model="caseForm.tags" aria-label="用例标签" placeholder="逗号分隔，例如 卡纸, 澄清" /></label><fieldset><legend>必评指标 <span class="required">*</span></legend><div class="checkbox-grid"><label v-for="metric in metrics" :key="metric[0]"><input v-model="caseForm.required_metrics" type="checkbox" :value="metric[0]" />{{ metric[1] }}</label></div><small>所选指标缺少证据时不能判为通过。</small></fieldset>
      <details class="advanced-fields"><summary>执行前置条件与数据来源</summary><div class="form-grid"><label>设备引用<input v-model="caseForm.device_ref" aria-label="设备引用" maxlength="200" placeholder="可选的显式设备引用" /></label><label>设备订单 ID<input v-model="caseForm.order_id" aria-label="设备订单 ID" inputmode="numeric" placeholder="可选的正整数" /></label></div><label>前置轮次<textarea v-model="caseForm.prior_turns" aria-label="前置轮次" rows="3" placeholder="每行一个显式用户输入，最多 19 行" /></label><label>来源说明（JSON 对象）<textarea v-model="caseForm.source" aria-label="来源说明" class="mono" rows="4" spellcheck="false" /></label><div class="form-grid"><label>许可<input v-model="caseForm.license" aria-label="许可" maxlength="100" /></label><label class="check-field"><input v-model="caseForm.seen_regression" type="checkbox" aria-label="已见回归题" /> 已见回归题</label></div></details><div class="form-actions"><ElButton :disabled="saving" @click="caseModal = false">取消</ElButton><ElButton type="primary" native-type="submit" :loading="saving">保存用例</ElButton></div></form></FormModal>
</template>

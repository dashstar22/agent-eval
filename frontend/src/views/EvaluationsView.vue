<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { ElOption, ElSelect } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'
import { api, problem } from '../api'
import type { ApiProblem, Evaluation } from '../types'
import { categoryNames, query, sourceNames } from '../workbench'
import AsyncState from '../components/AsyncState.vue'
import CaseDetailPanel from '../components/CaseDetailPanel.vue'
import PageHeading from '../components/PageHeading.vue'
import RunPicker from '../components/RunPicker.vue'
import StatusBadge from '../components/StatusBadge.vue'

const props = defineProps<{ badcases?: boolean }>()
const route = useRoute(), router = useRouter()
const rows = ref<Evaluation[]>([]), total = ref(0), loading = ref(true), refreshing = ref(false), error = ref<ApiProblem | null>(null)
const category = ref<string[]>([]), conclusion = ref(''), manual = ref(''), issue = ref('')
const listRegion = ref<HTMLElement>(), detailRegion = ref<HTMLElement>()
const run = computed(() => query(route.query.run_id) || query(route.query.run))
const score = computed(() => query(route.query.score_set_id) || query(route.query.score_set))
const caseId = computed(() => query(route.query.case_run_id) || query(route.query.case))
const page = computed(() => {
  const value = Number(query(route.query.page))
  return Number.isInteger(value) && value > 0 ? value : 1
})
const selectedIndex = computed(() => rows.value.findIndex(row => row.id === caseId.value))
const conclusionOptions = [
  { value: '', label: '全部' }, { value: 'FAIL', label: '失败' },
  { value: 'UNDETERMINED', label: '未判定' }, { value: 'PASS', label: '通过' },
]
const listQueryKey = computed(() => JSON.stringify([
  props.badcases, run.value, score.value, page.value, query(route.query.category), query(route.query.conclusion),
  query(route.query.manual_tag), query(route.query.issue), query(route.query.metric), query(route.query.case_run_ids),
]))
let generation = 0

async function load(preserveRows = false) {
  const current = ++generation
  loading.value = !preserveRows
  refreshing.value = preserveRows
  error.value = null
  if (!preserveRows) { rows.value = []; total.value = 0 }
  category.value = query(route.query.category).split(',').filter(Boolean)
  conclusion.value = query(route.query.conclusion)
  manual.value = query(route.query.manual_tag)
  issue.value = query(route.query.issue)
  try {
    const read = props.badcases ? api.badcases : api.evaluations
    const data = await read({
      run_id: run.value || undefined, score_set_id: score.value || undefined,
      category: category.value.join(',') || undefined,
      conclusion: !props.badcases ? conclusion.value || undefined : undefined,
      manual_tag: props.badcases ? manual.value || undefined : undefined,
      issue: props.badcases ? issue.value || undefined : undefined,
      metric: query(route.query.metric) || undefined,
      case_run_ids: query(route.query.case_run_ids) || undefined,
      page: page.value,
    })
    if (current === generation) { rows.value = data.items; total.value = data.total }
  } catch (e) {
    if (current === generation) error.value = problem(e)
  } finally {
    if (current === generation) { loading.value = false; refreshing.value = false }
  }
}

function filter() {
  void router.replace({ query: {
    ...route.query, category: category.value.filter(Boolean).join(',') || undefined,
    conclusion: !props.badcases ? conclusion.value || undefined : undefined,
    manual_tag: props.badcases ? manual.value || undefined : undefined,
    issue: props.badcases ? issue.value || undefined : undefined,
    page: undefined, case_run_id: undefined, case: undefined,
  } })
}
function setConclusion(value: string) { conclusion.value = value; filter() }
function pick(id: string, selected: string) {
  void router.replace({ query: { run_id: id || undefined, score_set_id: selected || undefined } })
}
function open(row: Evaluation) {
  void router.replace({ query: { ...route.query, case_run_id: row.id, case: undefined, score_set_id: score.value || undefined } })
}
async function close() {
  await router.replace({ query: { ...route.query, case_run_id: undefined, case: undefined } })
  await nextTick()
  listRegion.value?.focus({ preventScroll: true })
}
function scoreChange(id: string) {
  void router.replace({ query: { ...route.query, score_set_id: id, score_set: undefined } })
}
function changePage(value: number) {
  void router.replace({ query: { ...route.query, page: String(value), case_run_id: undefined, case: undefined } })
}
function adjacent(offset: number) {
  const row = rows.value[selectedIndex.value + offset]
  if (selectedIndex.value >= 0 && row) open(row)
}
watch(listQueryKey, () => { void load() }, { immediate: true })
watch(caseId, async value => {
  if (!value) return
  await nextTick()
  detailRegion.value?.focus({ preventScroll: true })
  if (window.matchMedia('(max-width: 1199px)').matches) {
    detailRegion.value?.scrollIntoView({ behavior: 'instant', block: 'start' })
  }
}, { immediate: true })
</script>

<template>
  <PageHeading
    :title="badcases ? 'Badcase 与人工复核' : '评测详情'"
    :description="badcases ? '失败原因可重复归类；评分故障与缺失观测单独保留。' : '从质量结论进入同一冻结记录，核对失败理由、实际回答和执行证据。'"
  >
    <template #actions>
      <RouterLink v-if="run" class="text-link" :to="{ path: '/reports', query: { run_id: run, score_set_id: score || undefined } }">查看报告 →</RouterLink>
      <ElButton :loading="refreshing" @click="load()">刷新列表</ElButton>
    </template>
  </PageHeading>

  <section class="panel evaluations-controls">
    <div class="panel-body">
      <RunPicker :run-id="run" :score-set-id="score" compact allow-empty @change="pick" />
      <div v-if="route.query.case_run_ids || route.query.metric" class="drilldown-notice">
        <span>来自报告钻取：{{ route.query.metric || '指定参与统计的用例集合' }}。</span>
        <RouterLink class="text-link" :to="{ path: route.path, query: { run_id: run || undefined, score_set_id: score || undefined } }">清除钻取条件</RouterLink>
      </div>
    </div>
    <form class="evaluation-filters" @submit.prevent="filter">
      <div v-if="!badcases" class="conclusion-filters" aria-label="质量结论筛选">
        <button v-for="item in conclusionOptions" :key="item.value" type="button" :aria-pressed="query(route.query.conclusion) === item.value" @click="setConclusion(item.value)">{{ item.label }}</button>
      </div>
      <label class="category-filter">
        <span>自动原因</span>
        <ElSelect v-model="category" multiple collapse-tags collapse-tags-tooltip clearable placeholder="全部分类" aria-label="自动分类多选">
          <ElOption v-for="(label, key) in categoryNames" :key="key" :value="String(key)" :label="label" />
        </ElSelect>
      </label>
      <template v-if="badcases">
        <label><span>人工原因</span><input v-model="manual" placeholder="按原因精确筛选" /></label>
        <label><span>评测异常</span><select v-model="issue">
          <option value="">全部（含评分故障）</option>
          <option value="JUDGE_ERROR">Judge 评分故障</option>
          <option value="SCORING_PENDING">评分尚未完成</option>
          <option value="NOT_EXECUTED">尚未执行</option>
          <option value="REQUIRED_METRIC_ERROR">必评指标错误</option>
          <option value="REQUIRED_METRIC_NA">必评证据不足</option>
          <option value="FULL_CONTEXT_UNAVAILABLE">完整上下文不可用</option>
        </select></label>
      </template>
      <ElButton native-type="submit">应用筛选</ElButton>
    </form>
  </section>

  <div class="evaluation-layout" :class="{ 'has-detail': caseId }">
    <section ref="listRegion" class="panel evaluations-list" tabindex="-1" aria-label="冻结用例列表">
      <div class="panel-heading"><h2>冻结用例</h2><span class="small muted" aria-live="polite">{{ loading ? '读取中' : `${total} 条符合条件` }}</span></div>
      <AsyncState
        :loading="loading" :error="error" :empty="!rows.length"
        :empty-title="badcases ? '没有符合筛选条件的 Badcase 或评测异常' : '没有符合条件的执行记录'"
        empty-text="可更换运行、评分集或筛选条件；尚未运行时请先创建实验。" @retry="load()"
      >
        <template #empty-action><RouterLink class="text-link" to="/experiments">前往实验</RouterLink></template>
        <div v-if="caseId" class="case-list">
          <article v-for="row in rows" :key="`${row.id}-${score}`" class="case-list-item" :class="{ 'is-selected': row.id === caseId }">
            <button type="button" class="case-select" :aria-pressed="row.id === caseId" @click="open(row)">
              <span class="case-select-title"><strong class="mono">{{ row.case_key }}</strong><StatusBadge :value="row.conclusion" domain="quality" /></span>
              <span class="list-case-question">{{ row.question }}</span>
            </button>
            <div class="list-case-facts"><StatusBadge :value="row.agent_status" domain="execution" /><span>{{ sourceNames[row.source_type] || row.source_type }}</span></div>
            <div v-if="row.automatic_tags.length || row.evaluation_issues.length" class="tag-list">
              <ElTag v-for="tag in row.automatic_tags" :key="tag" type="danger" size="small">{{ categoryNames[tag] || tag }}</ElTag>
              <ElTag v-for="item in row.evaluation_issues" :key="item" type="warning" size="small">{{ item }}</ElTag>
            </div>
            <div v-if="badcases" class="list-annotation"><div class="tag-list"><ElTag v-for="tag in row.annotation?.manual_tags || []" :key="tag" size="small" type="info">{{ tag }}</ElTag></div><p>{{ row.annotation?.note || '尚未标记' }}</p><button type="button" class="text-link" @click="open(row)">查看并标记</button></div>
          </article>
        </div>
        <div v-else class="table-scroll">
          <table><thead><tr><th>冻结用例</th><th>质量结论</th><th>执行 / 来源</th><th>自动原因与评测异常</th><th v-if="badcases">人工原因</th></tr></thead>
            <tbody><tr v-for="row in rows" :key="`${row.id}-${score}`">
              <td><button type="button" class="text-link record-title mono" @click="open(row)">{{ row.case_key }}</button><p class="table-subtext">{{ row.question }}</p></td>
              <td><StatusBadge :value="row.conclusion" domain="quality" /></td>
              <td><StatusBadge :value="row.agent_status" domain="execution" /><p class="table-subtext">{{ sourceNames[row.source_type] || row.source_type }}</p></td>
              <td><div class="tag-list"><ElTag v-for="tag in row.automatic_tags" :key="tag" type="danger" size="small">{{ categoryNames[tag] || tag }}</ElTag><ElTag v-for="item in row.evaluation_issues" :key="item" type="warning" size="small">{{ item }}</ElTag><span v-if="!row.automatic_tags.length && !row.evaluation_issues.length" class="small muted">无失败或异常标签</span></div></td>
              <td v-if="badcases"><div class="tag-list"><ElTag v-for="tag in row.annotation?.manual_tags || []" :key="tag" size="small" type="info">{{ tag }}</ElTag></div><p class="table-subtext">{{ row.annotation?.note || '尚未标记' }}</p><button type="button" class="text-link" @click="open(row)">查看并标记</button></td>
            </tr></tbody>
          </table>
        </div>
        <div v-if="total > 20" class="pagination"><ElPagination :current-page="page" :total="total" :page-size="20" :pager-count="5" layout="prev, pager, next" @current-change="changePage" /></div>
      </AsyncState>
    </section>
    <div v-if="caseId" ref="detailRegion" class="case-detail-region" tabindex="-1" aria-label="当前用例详情">
      <div class="detail-navigation">
        <ElButton @click="close">返回用例列表</ElButton>
        <div class="adjacent-actions"><ElButton :disabled="loading || selectedIndex <= 0" @click="adjacent(-1)">本页上一条</ElButton><ElButton :disabled="loading || selectedIndex < 0 || selectedIndex >= rows.length - 1" @click="adjacent(1)">本页下一条</ElButton></div>
      </div>
      <p v-if="!loading && !error && selectedIndex < 0" class="detail-link-note">当前用例不在本页列表中，详情仍按链接中的冻结记录读取。</p>
      <CaseDetailPanel :key="`${caseId}:${score}`" :case-id="caseId" :score-set-id="score" @score-change="scoreChange" @saved="load(true)" />
    </div>
  </div>
</template>

<style scoped>
.evaluations-controls { margin-bottom: var(--space-6); }
.drilldown-notice { display: flex; gap: var(--space-3); flex-wrap: wrap; padding-top: var(--space-4); color: var(--muted); font-size: var(--text-xs); }
.evaluation-filters { display: flex; align-items: flex-end; flex-wrap: wrap; gap: var(--space-4); padding: var(--space-4) var(--space-6); border-top: 1px solid var(--line); background: var(--subtle); }
.evaluation-filters label { display: grid; gap: var(--space-2); color: var(--muted); font-size: var(--text-xs); max-width: 100%; }
.evaluation-filters input, .evaluation-filters select { height: var(--control-height); font-size: var(--text-sm); max-width: 100%; }
.category-filter { width: 240px; }
.category-filter :deep(.el-select__wrapper) { min-height: var(--control-height); }
.conclusion-filters { display: flex; gap: var(--space-1); flex-wrap: wrap; align-items: center; margin-right: auto; }
.conclusion-filters button { border: 1px solid transparent; border-radius: var(--radius-control); color: var(--muted); padding: var(--space-2) var(--space-3); background: transparent; min-height: var(--control-height); }
.conclusion-filters button[aria-pressed=true] { color: var(--blue); background: var(--blue-soft); border-color: var(--blue); }
.evaluation-layout { display: grid; grid-template-columns: minmax(0, 1fr); gap: var(--space-6); align-items: start; }
.evaluation-layout.has-detail { grid-template-columns: minmax(260px, .8fr) minmax(0, 2fr); }
.evaluations-list, .case-detail-region { min-width: 0; scroll-margin-top: var(--space-6); }
.evaluations-list:focus, .case-detail-region:focus { outline: none; }
.evaluations-list .panel-heading { padding: var(--space-4) var(--space-5); flex-wrap: wrap; }
.evaluations-list table { min-width: 740px; }
.evaluations-list .table-subtext { font-size: var(--text-xs); color: var(--muted); }
.evaluations-list .record-title { font-size: var(--text-md); }
.case-list-item { padding: var(--space-4); border-bottom: 1px solid var(--line); border-left: 3px solid transparent; display: grid; gap: var(--space-3); }
.case-list-item:last-child { border-bottom: 0; }
.case-list-item.is-selected { border-left-color: var(--blue); background: var(--blue-soft); }
.case-select { display: grid; gap: var(--space-2); width: 100%; border: 0; padding: 0; text-align: left; color: var(--ink); background: transparent; }
.case-select-title { display: flex; justify-content: space-between; gap: var(--space-2); align-items: baseline; flex-wrap: wrap; }
.case-select-title strong { font-size: var(--text-md); }
.list-case-question { font-size: var(--text-sm); line-height: 1.65; overflow-wrap: anywhere; }
.list-case-facts { display: flex; gap: var(--space-2); align-items: center; flex-wrap: wrap; color: var(--muted); font-size: var(--text-xs); }
.list-annotation { display: grid; gap: var(--space-2); font-size: var(--text-xs); color: var(--muted); overflow-wrap: anywhere; }
.detail-navigation { display: flex; gap: var(--space-3); justify-content: space-between; flex-wrap: wrap; margin-bottom: var(--space-4); }
.adjacent-actions { display: flex; gap: var(--space-2); flex-wrap: wrap; }
.adjacent-actions .el-button + .el-button { margin-left: 0; }
.detail-link-note { font-size: var(--text-xs); color: var(--muted); margin-bottom: var(--space-3); }
.has-detail .pagination { padding: var(--space-3); justify-content: center; }
@media (max-width: 1199px) {
  .evaluation-layout.has-detail { grid-template-columns: minmax(0, 1fr); }
  .has-detail .evaluations-list { display: none; }
}
@media (max-width: 760px) {
  .evaluation-filters { padding: var(--space-4); gap: var(--space-3); }
  .conclusion-filters { width: 100%; }
  .category-filter { width: 100%; }
  .evaluation-filters label { width: 100%; }
  .detail-navigation .el-button { padding-left: var(--space-3); padding-right: var(--space-3); }
}
</style>

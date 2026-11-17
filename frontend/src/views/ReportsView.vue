<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, problem } from '../api'
import type { ApiProblem, Report } from '../types'
import { date, pretty, query } from '../workbench'
import AsyncState from '../components/AsyncState.vue'
import RunPicker from '../components/RunPicker.vue'
import SummaryPanel from '../components/SummaryPanel.vue'
import VersionPanel from '../components/VersionPanel.vue'
import ReportChart from '../components/ReportChart.vue'
import PageHeading from '../components/PageHeading.vue'
import StatusBadge from '../components/StatusBadge.vue'
import ResultSummary from '../components/ResultSummary.vue'
import EvidenceNotice from '../components/EvidenceNotice.vue'
import SectionCard from '../components/SectionCard.vue'

const route = useRoute(), router = useRouter()
const report = ref<Report | null>(null), loading = ref(false), error = ref<ApiProblem | null>(null)
const run = computed(() => query(route.query.run_id) || query(route.query.run))
const score = computed(() => query(route.query.score_set_id) || query(route.query.score_set))
const compareRun = computed(() => query(route.query.compare_run_id) || query(route.query.compare))
const compareScore = computed(() => query(route.query.compare_score_set_id))
const comparisonExpanded = ref(Boolean(compareRun.value)), evidenceOpen = ref(false)
const evidenceElement = ref<HTMLDetailsElement>()
const evaluationsLink = (conclusion?: string) => ({ path: '/evaluations', query: {
  run_id: report.value?.run.id || run.value,
  score_set_id: report.value?.score_set.id || score.value || undefined,
  conclusion,
} })
let generation = 0

async function load() {
  const current = ++generation
  report.value = null; error.value = null
  if (!run.value) { loading.value = false; return }
  loading.value = true
  try {
    const result = await api.report(run.value, score.value, compareRun.value, compareScore.value)
    if (current === generation) report.value = result
  } catch (e) { if (current === generation) error.value = problem(e) }
  finally { if (current === generation) loading.value = false }
}
function pick(id: string, selected: string) {
  void router.replace({ query: { ...route.query, run_id: id || undefined, score_set_id: selected || undefined, run: undefined, score_set: undefined } })
}
function pickCompare(id: string, selected: string) {
  void router.replace({ query: { ...route.query, compare_run_id: id || undefined, compare_score_set_id: selected || undefined, compare: undefined } })
}
function removeComparison() {
  comparisonExpanded.value = false
  void router.replace({ query: { ...route.query, compare_run_id: undefined, compare_score_set_id: undefined, compare: undefined } })
}
async function showEvidence() {
  evidenceOpen.value = true
  await nextTick()
  evidenceElement.value?.scrollIntoView({ block: 'start', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' })
  evidenceElement.value?.querySelector('summary')?.focus({ preventScroll: true })
}
watch(compareRun, value => { if (value) comparisonExpanded.value = true })
watch(() => route.fullPath, load, { immediate: true })
</script>

<template>
  <div class="report-page">
    <PageHeading title="评测报告" description="先查看质量结果，再从失败用例核对理由与证据。">
      <template #actions>
        <RouterLink v-if="report && report.summary.F > 0" class="report-primary-action" :to="evaluationsLink('FAIL')">查看失败用例 {{ report.summary.F }} →</RouterLink>
        <ElButton :disabled="!run" :loading="loading" @click="load">刷新报告</ElButton>
      </template>
    </PageHeading>

    <section class="panel report-context" aria-label="报告运行与评分集">
      <RunPicker :run-id="run" :score-set-id="score" label="报告运行" compact @change="pick" />
      <ElButton v-if="!comparisonExpanded" @click="comparisonExpanded = true">添加对照</ElButton>
      <ElButton v-else @click="removeComparison">取消对照</ElButton>
      <div v-if="comparisonExpanded" class="report-compare-picker">
        <RunPicker :run-id="compareRun" :score-set-id="compareScore" label="对照运行" compact allow-empty @change="pickCompare" />
        <p class="small muted">相同数据与评分口径才计算共同有效样本的差异。</p>
      </div>
    </section>

    <AsyncState :loading="loading" :error="error" :empty="!run" empty-title="选择一条运行查看报告" empty-text="报告只展示持久化结果，没有运行时不会生成示例成绩。" @retry="load">
      <template #empty-action><RouterLink class="text-link" to="/experiments">前往实验创建运行</RouterLink></template>
      <div v-if="report" class="report-content">
        <div class="report-run-context">
          <span>运行 <StatusBadge :value="report.run.status" domain="run" /></span>
          <span>{{ report.score_set.kind }} <StatusBadge :value="report.score_set.status" domain="scoring" /></span>
          <span>{{ date(report.run.created_at) }}</span>
          <span class="mono report-run-id">{{ report.run.id }}</span>
          <RouterLink class="text-link" :to="evaluationsLink()">查看全部用例 →</RouterLink>
        </div>
        <section class="panel report-results">
          <ResultSummary :summary="report.summary" :run-id="report.run.id" :score-set-id="report.score_set.id" />
          <RouterLink v-if="report.summary.U > 0" class="text-link report-undetermined-link" :to="evaluationsLink('UNDETERMINED')">查看未判定用例 {{ report.summary.U }} →</RouterLink>
        </section>

        <EvidenceNotice
          :tone="report.source_scope.real_quality_evidence ? 'info' : 'warning'"
          :title="report.source_scope.real_quality_evidence ? '本运行有已核验的真实 Agent 与 Judge 质量证据' : '本运行不满足完整真实质量证据条件'"
          :description="`实际版本核验：${report.version_verification.status}。来源声明、模拟与协议替身范围分别保留。`"
        ><template #actions><button type="button" class="text-link" @click="showEvidence">查看核验范围 ↓</button></template></EvidenceNotice>

        <div class="report-primary-grid">
          <div class="report-failures">
            <p class="report-section-caption">自动原因可重复归类，也可能包含质量通过用例；分类计数不等同质量失败 F。</p>
            <ReportChart :chart="report.charts.badcase_categories" />
          </div>
          <SectionCard title="质量与执行指标" description="各项保留有效样本、分母和未知范围。">
            <SummaryPanel :summary="report.summary" mode="metrics" />
          </SectionCard>
        </div>

        <section v-if="report.comparison" class="report-comparison" aria-label="运行对照与共同样本">
          <EvidenceNotice :tone="report.comparison.comparable ? 'info' : 'warning'" :title="report.comparison.comparable ? '相同数据与评分口径，使用逐指标共同有效样本比较' : '数据或评分口径不同，仅并排展示'">
            <div class="comparison-explanation">
              <ul v-if="report.comparison.limitations.length" class="limitation-list"><li v-for="reason in report.comparison.limitations" :key="reason">{{ reason }}</li></ul>
              <p class="small">实际版本：{{ report.comparison.version_comparison.status }}。差异不自动代表统计显著或线上效果提升。</p>
              <RouterLink class="text-link" :to="{ path: '/experiments', query: { left: report.run.id, right: compareRun, left_score: report.score_set.id, right_score: compareScore } }">查看比较条件与逐题差异 →</RouterLink>
            </div>
          </EvidenceNotice>
          <div class="report-secondary-grid">
            <ReportChart :chart="report.charts.experiment_comparison" comparison :paired="report.comparison.comparable" />
            <ReportChart :chart="report.charts.metric_changes" difference :paired="report.comparison.comparable" />
          </div>
        </section>

        <ReportChart :chart="report.charts.latency_distribution" />

        <details ref="evidenceElement" class="panel report-evidence" :open="evidenceOpen" @toggle="evidenceOpen = ($event.target as HTMLDetailsElement).open">
          <summary>运行与证据 <span class="small muted">来源、版本、执行统计、模型用量与冻结条件</span></summary>
          <div class="panel-body">
            <section>
              <h2>来源与核验范围</h2>
              <p class="small muted">{{ report.source_scope.real_quality_evidence ? '本运行有已核验的真实 Agent 与 Judge 质量证据。' : '本运行不满足完整真实质量证据条件；请核对模拟、协议替身与实际模型核验范围。' }}</p>
              <details class="raw-detail"><summary>查看来源计数与覆盖范围</summary><pre>{{ pretty(report.source_scope) }}</pre></details>
            </section>
            <SummaryPanel :summary="report.summary" mode="details" />
            <VersionPanel :value="report.version_verification" />
            <details class="raw-detail"><summary>当前评分集与冻结条件</summary><pre>{{ pretty({ score_set: report.score_set, snapshot_sha256: report.run.snapshot_sha256, snapshot: report.run.snapshot }) }}</pre></details>
          </div>
        </details>
      </div>
    </AsyncState>
  </div>
</template>

<style scoped>
.report-page { min-width: 0; }
.report-primary-action { display: inline-flex; min-height: var(--control-height); align-items: center; justify-content: center; padding: var(--space-2) var(--space-4); border-radius: var(--radius-control); color: var(--paper); background: var(--blue); font-weight: 600; }
.report-primary-action:hover { background: color-mix(in srgb, var(--blue) 88%, var(--navy)); }
.report-context { padding: var(--space-5); display: flex; gap: var(--space-4); flex-wrap: wrap; align-items: flex-end; margin-bottom: var(--space-6); }
.report-context > :deep(.run-picker) { flex: 1; }
.report-compare-picker { flex: 0 0 100%; border-top: 1px solid var(--line); padding-top: var(--space-4); display: grid; gap: var(--space-2); }
.report-content { display: grid; gap: var(--space-6); }
.report-run-context { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-2) var(--space-4); color: var(--muted); font-size: var(--text-xs); }
.report-run-context > span { display: inline-flex; align-items: center; gap: var(--space-2); min-width: 0; }
.report-run-context > .text-link { margin-left: auto; }
.report-run-id { overflow-wrap: anywhere; }
.report-results { padding: var(--space-6); }
.report-undetermined-link { display: inline-block; margin-top: var(--space-4); }
.report-primary-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.15fr); gap: var(--space-6); align-items: start; }
.report-failures { min-width: 0; display: flex; flex-direction: column; gap: var(--space-2); }
.report-section-caption { color: var(--muted); font-size: var(--text-xs); order: 1; }
.report-comparison { display: grid; gap: var(--space-4); }
.comparison-explanation { flex-basis: 100%; display: grid; gap: var(--space-2); }
.comparison-explanation .limitation-list { margin: 0; }
.report-secondary-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-6); }
.report-evidence > summary { padding: var(--space-5) var(--space-6); font-size: var(--text-md); font-weight: 600; }
.report-evidence > summary > span { margin-left: var(--space-2); font-weight: 400; }
.report-evidence[open] > summary { border-bottom: 1px solid var(--line); }
.report-evidence > .panel-body { display: grid; gap: var(--space-6); }
.report-evidence :deep(.version-panel) { margin-top: 0; }
.report-evidence .raw-detail { margin-top: var(--space-3); }
@media(max-width: 1150px) { .report-primary-grid, .report-secondary-grid { grid-template-columns: 1fr; } }
@media(max-width: 760px) {
  .report-context, .report-results { padding: var(--space-4); }
  .report-run-context > .text-link { margin-left: 0; }
  .report-evidence > summary { padding: var(--space-4); }
  .report-evidence > summary > span { display: block; margin-left: 0; margin-top: var(--space-1); }
}
</style>

<script setup lang="ts">
import { onBeforeUnmount, reactive, ref, watch } from 'vue'
import { problem, validation } from '../../api'
import { extensions } from '../../api/extensions'
import type { ApiProblem } from '../../types'
import type { RegressionCheck } from '../../types/extensions'
import { number, percent, pretty } from '../../workbench'
import RunPicker from '../RunPicker.vue'
import ProblemBox from '../ProblemBox.vue'
import SectionCard from '../SectionCard.vue'
import StatusBadge from '../StatusBadge.vue'
import VersionPanel from '../VersionPanel.vue'

const props = defineProps<{ runId: string; scoreSetId: string }>()
const baseline = ref(''), baselineScore = ref(''), policy = reactive({ max_new_failures: 0, min_coverage: 100, max_pass_rate_drop: 0 })
const loading = ref(false), error = ref<ApiProblem | null>(null), result = ref<RegressionCheck | null>(null)
let generation = 0, disposed = false
function pick(id: string, score: string) { baseline.value = id; baselineScore.value = score }
function checkValue(id: string, value: number | null) {
  if (value === null) return '不可用'
  if (id === 'coverage') return percent(value)
  if (id === 'pass_rate_drop') return `${(value * 100).toFixed(1)} 个百分点`
  return number(value, 4)
}
function reset() { generation++; result.value = null; loading.value = false; error.value = null }
async function check() {
  if (loading.value) return
  error.value = validation([
    ...(!props.runId || !baseline.value ? [{ field: 'runs', message: '选择基线和候选运行。' }] : []),
    ...(props.runId === baseline.value && props.scoreSetId === baselineScore.value ? [{ field: 'candidate', message: '基线与候选不能是同一运行和同一评分集。' }] : []),
    ...(!Number.isInteger(policy.max_new_failures) || policy.max_new_failures < 0 ? [{ field: 'max_new_failures', message: '允许新增失败数为非负整数。' }] : []),
    ...(!Number.isFinite(policy.min_coverage) || policy.min_coverage < 0 || policy.min_coverage > 100 ? [{ field: 'min_coverage', message: '最低覆盖率需在 0—100% 之间。' }] : []),
    ...(!Number.isFinite(policy.max_pass_rate_drop) || policy.max_pass_rate_drop < 0 || policy.max_pass_rate_drop > 100 ? [{ field: 'max_pass_rate_drop', message: '允许通过率下降需在 0—100 个百分点之间。' }] : []),
  ])
  if (error.value) return
  const current = ++generation
  loading.value = true; result.value = null
  try {
    const data = await extensions.regression({ run_id: baseline.value, score_set_id: baselineScore.value || undefined }, { run_id: props.runId, score_set_id: props.scoreSetId || undefined }, { max_new_failures: policy.max_new_failures, min_coverage: policy.min_coverage / 100, max_pass_rate_drop: policy.max_pass_rate_drop / 100 })
    if (!disposed && current === generation) result.value = data
  } catch (e) { if (!disposed && current === generation) error.value = problem(e) }
  finally { if (!disposed && current === generation) loading.value = false }
}
watch(() => [props.runId, props.scoreSetId, baseline.value, baselineScore.value, policy.max_new_failures, policy.min_coverage, policy.max_pass_rate_drop], reset)
onBeforeUnmount(() => { disposed = true; generation++ })
</script>

<template>
  <div class="quality-stack">
    <SectionCard title="基线回归检查" description="只比较保存结果。冻结条件、完成状态和覆盖要求分别核对，条件不足时无法判为通过。">
      <form class="quality-form" @submit.prevent="check"><RunPicker :run-id="baseline" :score-set-id="baselineScore" label="基线运行" compact @change="pick" /><div class="quality-fields"><label>允许新增失败数<input v-model.number="policy.max_new_failures" type="number" min="0" step="1" /></label><label>最低确定覆盖率（%）<input v-model.number="policy.min_coverage" type="number" min="0" max="100" step="1" /></label><label>允许有效通过率下降（百分点）<input v-model.number="policy.max_pass_rate_drop" type="number" min="0" max="100" step="0.1" /></label></div><p class="small muted">默认：新增失败 0、最低覆盖率 100%、通过率不下降。基线在左，候选在右。</p><ProblemBox :error="error" /><div><ElButton native-type="submit" type="primary" :loading="loading">检查已保存结果</ElButton></div></form>
    </SectionCard>
    <SectionCard v-if="result" title="回归检查结果">
      <div class="quality-stack"><div class="quality-summary" :class="result.verdict === 'PASS' ? 'quality-summary--pass' : result.verdict === 'FAIL' ? 'quality-summary--fail' : 'quality-summary--warning'"><div class="quality-record-title"><strong>{{ result.verdict === 'PASS' ? '通过当前回归策略' : result.verdict === 'FAIL' ? '未通过当前回归策略' : '无法判断是否通过' }}</strong><StatusBadge :value="result.verdict" domain="quality" /></div><p class="small">判定基于保存结果与当前策略，不代表线上效果或统计显著性。</p></div>
        <div class="quality-facts"><span>运行与评分完成：{{ result.conditions.completed ? '是' : '否' }}</span><span>冻结口径可比：{{ result.conditions.comparable ? '是' : '否' }}</span><span>覆盖充足：{{ result.conditions.coverage_sufficient ? '是' : '否' }}</span></div>
        <ul v-if="result.conditions.limitations.length" class="limitation-list"><li v-for="item in result.conditions.limitations" :key="item">{{ item }}</li></ul>
        <div class="table-scroll"><table><thead><tr><th>检查项</th><th>结论</th><th>实际值</th><th>阈值</th><th>理由</th></tr></thead><tbody><tr v-for="item in result.checks" :key="item.id"><td>{{ item.label }}</td><td><StatusBadge :value="item.status" domain="quality" /></td><td>{{ checkValue(item.id, item.actual) }}</td><td>{{ checkValue(item.id, item.threshold) }}</td><td>{{ item.reason || '—' }}</td></tr></tbody></table></div>
        <div class="quality-facts"><span>基线 P/F/U：{{ result.comparison.left.summary.P }}/{{ result.comparison.left.summary.F }}/{{ result.comparison.left.summary.U }}</span><span>候选 P/F/U：{{ result.comparison.right.summary.P }}/{{ result.comparison.right.summary.F }}/{{ result.comparison.right.summary.U }}</span><span>基线有效通过率：{{ percent(result.comparison.left.summary.effective_pass_rate) }}</span><span>候选有效通过率：{{ percent(result.comparison.right.summary.effective_pass_rate) }}</span></div>
        <section v-for="group in [{ title: '新增失败', rows: result.new_failures }, { title: '已修复', rows: result.fixed_cases }, { title: '未知与无法配对', rows: result.unknown_cases }]" :key="group.title"><h3>{{ group.title }} · {{ group.rows.length }}</h3><p v-if="!group.rows.length" class="small muted">本次未列出此类题目。</p><div v-else class="table-scroll"><table><thead><tr><th>用例</th><th>基线 → 候选</th><th>配对说明</th><th>证据入口</th></tr></thead><tbody><tr v-for="row in group.rows" :key="row.case_key"><td class="mono">{{ row.case_key }}</td><td>{{ row.left_conclusion || '未知' }} → {{ row.right_conclusion || '未知' }}</td><td>{{ row.reason || (row.content_match ? '冻结内容匹配' : '冻结内容不匹配') }}</td><td><div class="quality-actions"><RouterLink v-if="row.left_case_run_id" class="text-link" :to="{ path: '/evaluations', query: { run_id: result.comparison.left.run.id, score_set_id: result.comparison.left.score_set.id, case_run_id: row.left_case_run_id } }">基线</RouterLink><RouterLink v-if="row.right_case_run_id" class="text-link" :to="{ path: '/evaluations', query: { run_id: result.comparison.right.run.id, score_set_id: result.comparison.right.score_set.id, case_run_id: row.right_case_run_id } }">候选</RouterLink></div></td></tr></tbody></table></div></section>
        <details class="raw-detail"><summary>实际版本核验与来源范围</summary><div class="quality-reading"><section><h3>基线</h3><VersionPanel :value="result.conditions.version_evidence.baseline" /></section><section><h3>候选</h3><VersionPanel :value="result.conditions.version_evidence.candidate" /></section></div><pre>{{ pretty({ version_comparison: result.conditions.version_evidence.comparison, source_scope: result.conditions.source_scope, policy: result.policy }) }}</pre></details>
      </div>
    </SectionCard>
  </div>
</template>

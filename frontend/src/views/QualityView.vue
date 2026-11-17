<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { query } from '../workbench'
import PageHeading from '../components/PageHeading.vue'
import RunPicker from '../components/RunPicker.vue'
import EvidenceNotice from '../components/EvidenceNotice.vue'
import QualityReviews from '../components/quality/QualityReviews.vue'
import QualityRegression from '../components/quality/QualityRegression.vue'
import QualityDrafts from '../components/quality/QualityDrafts.vue'
import QualityStability from '../components/quality/QualityStability.vue'
import '../quality.css'

const route = useRoute(), router = useRouter()
const tabs = [{ id: 'reviews', label: '人工复核' }, { id: 'regression', label: '回归检查' }, { id: 'drafts', label: '回归候选' }, { id: 'stability', label: '稳定性' }]
const tab = computed(() => tabs.some(item => item.id === query(route.query.tab)) ? query(route.query.tab) : 'reviews')
const run = computed(() => query(route.query.run_id)), score = computed(() => query(route.query.score_set_id)), caseId = computed(() => query(route.query.case_run_id))
function pick(runId: string, scoreSet: string) { void router.replace({ query: { ...route.query, run_id: runId || undefined, score_set_id: scoreSet || undefined, case_run_id: undefined } }) }
function selectCase(id: string) { void router.replace({ query: { ...route.query, case_run_id: id || undefined } }) }
</script>

<template>
  <div class="quality-page">
    <PageHeading title="质量工作台" description="复核已保存结果、检查回归，并把失败证据带回下一轮评测。"><template #actions><RouterLink v-if="run" class="text-link" :to="{ path: '/reports', query: { run_id: run, score_set_id: score || undefined } }">查看来源报告 →</RouterLink></template></PageHeading>
    <EvidenceNotice title="结果与证据各自保留" description="人工复核不会覆盖 Judge；分析保存结果不产生模型调用。重复运行需单独创建并沿用冻结预算。" />
    <nav class="quality-tabs" aria-label="质量工具"><RouterLink v-for="item in tabs" :key="item.id" :to="{ path: '/quality', query: { ...route.query, tab: item.id } }" :class="{ active: tab === item.id }" :aria-current="tab === item.id ? 'page' : undefined">{{ item.label }}</RouterLink></nav>
    <section class="panel quality-context"><RunPicker :run-id="run" :score-set-id="score" :label="tab === 'regression' ? '候选运行' : tab === 'stability' ? '重复运行来源 / 分析选择' : '来源运行'" compact @change="pick" /><p class="small muted">选择固定评分集以复查同一份结果。ORIGINAL 使用该运行的原评分。</p></section>
    <QualityReviews v-if="tab === 'reviews'" :run-id="run" :score-set-id="score" :case-run-id="caseId" @select-case="selectCase" />
    <QualityRegression v-else-if="tab === 'regression'" :run-id="run" :score-set-id="score" />
    <QualityDrafts v-else-if="tab === 'drafts'" :run-id="run" :score-set-id="score" :case-run-id="caseId" @select-case="selectCase" />
    <QualityStability v-else :run-id="run" :score-set-id="score" />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { Summary } from '../types'
import { percent } from '../workbench'

const props = defineProps<{ summary: Summary; runId?: string; scoreSetId?: string }>()
const outcomes = computed(() => [
  { key: 'PASS', label: '通过', count: props.summary.P, tone: 'success', hint: '满足必评条件' },
  { key: 'FAIL', label: '失败', count: props.summary.F, tone: 'danger', hint: '查看失败理由与依据' },
  { key: 'UNDETERMINED', label: '未判定', count: props.summary.U, tone: 'warning', hint: '与失败分别统计' },
])
const link = (conclusion: string) => ({
  path: '/evaluations',
  query: { run_id: props.runId, score_set_id: props.scoreSetId || undefined, conclusion },
})
</script>

<template>
  <section class="result-summary" aria-label="质量结果概览">
    <div class="result-heading"><h2>质量结果</h2><span class="small muted">冻结用例 {{ summary.N }} · N = P + F + U</span></div>
    <div class="result-outcomes">
      <div v-for="item in outcomes" :key="item.key" class="result-outcome" :class="`result-outcome--${item.tone}`">
        <RouterLink v-if="runId && item.count > 0" :to="link(item.key)" class="result-outcome-link" :aria-label="`查看${item.count}条${item.label}用例`">
          <span>{{ item.label }} <span aria-hidden="true">→</span></span><strong>{{ item.count }}</strong><small>{{ item.hint }}</small>
        </RouterLink>
        <div v-else class="result-outcome-static"><span>{{ item.label }}</span><strong>{{ item.count }}</strong><small>{{ item.hint }}</small></div>
      </div>
    </div>
    <div v-if="summary.N > 0" class="result-distribution" :aria-label="`共${summary.N}条，通过${summary.P}条，失败${summary.F}条，未判定${summary.U}条`">
      <template v-for="item in outcomes" :key="item.key">
        <RouterLink v-if="item.count > 0 && runId" :to="link(item.key)" :style="{ flexGrow: item.count }" :class="`result-segment result-segment--${item.tone}`" :aria-label="`查看${item.count}条${item.label}用例`" />
        <span v-else-if="item.count > 0" :style="{ flexGrow: item.count }" :class="`result-segment result-segment--${item.tone}`" />
      </template>
    </div>
    <p v-else class="small muted">没有冻结用例，比例不可用。</p>
    <div class="result-ratios">
      <div><span>有效通过率</span><strong>{{ percent(summary.effective_pass_rate) }}</strong><small>P/(P+F) · {{ summary.P }}/{{ summary.P + summary.F }}</small></div>
      <div><span>可判定覆盖率</span><strong>{{ percent(summary.determinate_coverage) }}</strong><small>(P+F)/N · {{ summary.P + summary.F }}/{{ summary.N }}</small></div>
    </div>
    <p v-if="summary.provisional || summary.interrupted" class="warning-line">{{ summary.interrupted ? '运行已中断，未执行项仍在冻结分母中。' : '运行尚未结束，当前为临时统计。' }}</p>
  </section>
</template>

<script setup lang="ts">
import type { Summary } from '../types'
import { metricNames, number, percent } from '../workbench'
import ResultSummary from './ResultSummary.vue'
import StatusBadge from './StatusBadge.vue'

withDefaults(defineProps<{ summary: Summary; mode?: 'all' | 'metrics' | 'details' }>(), { mode: 'all' })
</script>

<template>
  <div class="summary-panel">
    <ResultSummary v-if="mode === 'all'" :summary="summary" />
    <div v-if="mode !== 'details'" class="metric-grid" aria-label="质量与执行指标">
      <div v-for="(item, key) in summary.metric_averages" :key="key" class="metric-tile">
        <small>{{ metricNames[key] || key }}</small>
        <strong>{{ number(item.mean) }}<span v-if="item.mean !== null"> / 5</span></strong>
        <small>有效样本 {{ item.valid_count }}/{{ item.total_count }}</small>
      </div>
      <div class="metric-tile">
        <small>工具技术成功率</small><strong>{{ percent(summary.tools.success_rate) }}</strong>
        <small>{{ summary.tools.normal_completion }}/{{ summary.tools.denominator }} · 未知 {{ summary.tools.unknown }}</small>
      </div>
      <div class="metric-tile">
        <small>成功执行平均耗时</small>
        <strong>{{ number(summary.agent.success_duration_mean_ms) }}<span v-if="summary.agent.success_duration_mean_ms !== null"> ms</span></strong>
        <small>有效样本 {{ summary.agent.success_duration_valid_count }}/{{ summary.N }}</small>
      </div>
      <div class="metric-tile">
        <small>执行超时率</small><strong>{{ percent(summary.agent.timeout_rate) }}</strong>
        <small>{{ summary.agent.timeout_count }}/{{ summary.agent.timeout_denominator }} 次已执行</small>
      </div>
    </div>
    <div v-if="mode !== 'metrics'" class="summary-detail-grid">
      <section>
        <h3>执行与评分状态</h3>
        <dl class="fact-list">
          <dt>Agent 执行</dt>
          <dd><div class="status-counts"><span v-for="(count, status) in summary.agent.status_counts" :key="status"><StatusBadge :value="status" domain="execution" /> {{ count }}</span><span v-if="!Object.keys(summary.agent.status_counts).length">尚无执行状态</span></div></dd>
          <dt>Judge 评分</dt>
          <dd><div class="status-counts"><span v-for="(count, status) in summary.judge.status_counts" :key="status"><StatusBadge :value="status" domain="scoring" /> {{ count }}</span><span v-if="!Object.keys(summary.judge.status_counts).length">尚无评分状态</span></div></dd>
          <dt>工具结果</dt><dd>正常 {{ summary.tools.normal_completion }} · 业务拒绝 {{ summary.tools.business_refusal }} · 技术失败 {{ summary.tools.technical_failure }} · 未知 {{ summary.tools.unknown }}</dd>
          <dt>轨迹完整性</dt><dd><span v-for="(count, status) in summary.tools.trajectory_counts" :key="status" class="trajectory-count">{{ status }} {{ count }}</span><span v-if="!Object.keys(summary.tools.trajectory_counts).length">未提供</span></dd>
          <dt>工具统计来源</dt><dd>{{ summary.tools.sources.join('、') || '未提供' }}</dd>
          <template v-if="summary.tools.reason"><dt>统计范围</dt><dd>{{ summary.tools.reason }}</dd></template>
        </dl>
      </section>
      <section>
        <h3>模型用量与成本</h3>
        <div v-for="(usage, who) in summary.usage" :key="who" class="usage-record">
          <strong>{{ who }}</strong> <span class="muted small">{{ usage.availability }}{{ usage.known_part_only ? ' · 仅已知部分' : '' }}</span>
          <dl class="fact-list">
            <dt>输入 / 输出 Token</dt><dd>{{ number(usage.input_tokens) }} / {{ number(usage.output_tokens) }}</dd>
            <dt>总 Token</dt><dd>{{ number(usage.total_tokens) }} · 已知记录 {{ usage.known_total_tokens_records }}/{{ usage.applicable_records }}</dd>
            <dt>成本</dt><dd>{{ number(usage.cost) }} {{ usage.cost_currency || '' }}<small v-if="usage.cost_reason"> · {{ usage.cost_reason }}</small></dd>
          </dl>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.summary-panel { display: grid; gap: var(--space-6); min-width: 0; }
.summary-panel .metric-grid { grid-template-columns: repeat(auto-fit, minmax(min(100%, 150px), 1fr)); gap: var(--space-3); }
.summary-panel .metric-tile { background: var(--subtle); border: 1px solid var(--line); border-radius: var(--radius-control); padding: var(--space-4); }
.summary-panel .metric-tile small { font-size: var(--text-xs); color: var(--muted); line-height: 1.6; }
.summary-panel .metric-tile strong { font-family: var(--font-data); font-size: 24px; color: var(--ink); font-variant-numeric: tabular-nums; margin: var(--space-2) 0; }
.summary-panel .metric-tile strong span { font-size: var(--text-xs); color: var(--muted); }
.summary-panel .summary-detail-grid { margin-top: 0; gap: var(--space-6); }
.summary-panel .summary-detail-grid > section { min-width: 0; }
.summary-panel .fact-list { font-size: var(--text-xs); }
.summary-panel .fact-list dd { color: var(--ink); }
.status-counts { display: flex; flex-wrap: wrap; gap: var(--space-2) var(--space-3); }
.status-counts > span { white-space: nowrap; }
.trajectory-count { display: inline-block; margin-right: var(--space-3); }
.summary-panel .usage-record > strong { font: 600 var(--text-sm)/1.6 var(--font-ui); }
@media(max-width: 760px) { .summary-panel .summary-detail-grid { grid-template-columns: 1fr; } }
</style>

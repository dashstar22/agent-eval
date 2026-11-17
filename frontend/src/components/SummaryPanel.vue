<script setup lang="ts">
import type { Summary } from '../types'
import { metricNames, number, percent, pretty } from '../workbench'
defineProps<{ summary: Summary }>()
</script>
<template>
  <div class="summary-strip" aria-label="报告统计">
    <div><small>冻结用例 N</small><strong>{{ summary.N }}</strong></div>
    <div><small>通过 / 失败 / 未判定</small><strong>{{ summary.P }} <span>/ {{ summary.F }} / {{ summary.U }}</span></strong></div>
    <div><small>有效通过率 · P/(P+F)</small><strong>{{ percent(summary.effective_pass_rate) }}</strong><small>{{ summary.P }}/{{ summary.P + summary.F }}</small></div>
    <div><small>可判定覆盖率 · (P+F)/N</small><strong>{{ percent(summary.determinate_coverage) }}</strong><small>{{ summary.P + summary.F }}/{{ summary.N }}</small></div>
  </div>
  <p v-if="summary.provisional || summary.interrupted" class="warning-line">{{ summary.interrupted ? '运行已中断，未执行项仍在分母中。' : '临时统计，运行尚未结束。' }}</p>
  <div class="metric-grid">
    <div v-for="(item, key) in summary.metric_averages" :key="key" class="metric-tile"><small>{{ metricNames[key] || key }}</small><strong>{{ number(item.mean) }}<span v-if="item.mean !== null"> / 5</span></strong><small>有效样本 {{ item.valid_count }}/{{ item.total_count }}</small></div>
    <div class="metric-tile"><small>工具技术成功率</small><strong>{{ percent(summary.tools.success_rate) }}</strong><small>{{ summary.tools.normal_completion }}/{{ summary.tools.denominator }} · 未知 {{ summary.tools.unknown }}</small></div>
    <div class="metric-tile"><small>成功执行平均耗时</small><strong>{{ number(summary.agent.success_duration_mean_ms) }}<span v-if="summary.agent.success_duration_mean_ms !== null"> ms</span></strong><small>有效样本 {{ summary.agent.success_duration_valid_count }}/{{ summary.N }}</small></div>
    <div class="metric-tile"><small>执行超时率</small><strong>{{ percent(summary.agent.timeout_rate) }}</strong><small>{{ summary.agent.timeout_count }}/{{ summary.agent.timeout_denominator }} 次已执行</small></div>
  </div>
  <div class="summary-detail-grid">
    <div><h3>执行与评分状态</h3><dl class="fact-list"><dt>Agent 执行</dt><dd>{{ pretty(summary.agent.status_counts) }}</dd><dt>Judge 评分</dt><dd>{{ pretty(summary.judge.status_counts) }}</dd><dt>工具结果</dt><dd>正常 {{ summary.tools.normal_completion }} · 业务拒绝 {{ summary.tools.business_refusal }} · 技术失败 {{ summary.tools.technical_failure }} · 未知 {{ summary.tools.unknown }}</dd><dt>轨迹完整性</dt><dd>{{ pretty(summary.tools.trajectory_counts) }}</dd><dt>工具统计来源</dt><dd>{{ summary.tools.sources.join('、') }}</dd></dl></div>
    <div><h3>模型用量与成本</h3><div v-for="(usage, who) in summary.usage" :key="who" class="usage-record"><strong>{{ who }}</strong> <span class="muted small">{{ usage.availability }}{{ usage.known_part_only ? ' · 仅已知部分' : '' }}</span><dl class="fact-list"><dt>输入 / 输出 Token</dt><dd>{{ number(usage.input_tokens) }} / {{ number(usage.output_tokens) }}</dd><dt>总 Token</dt><dd>{{ number(usage.total_tokens) }} · 已知记录 {{ usage.known_total_tokens_records }}/{{ usage.applicable_records }}</dd><dt>成本</dt><dd>{{ number(usage.cost) }} {{ usage.cost_currency || '' }}<small v-if="usage.cost_reason"> · {{ usage.cost_reason }}</small></dd></dl></div></div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import * as echarts from 'echarts/core'
import { BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent, AriaComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { ChartDatum, ReportChart } from '../types'
import { categoryNames, metricNames, number } from '../workbench'

echarts.use([BarChart, GridComponent, TooltipComponent, LegendComponent, AriaComponent, CanvasRenderer])
const props = defineProps<{ chart: ReportChart; comparison?: boolean; paired?: boolean; difference?: boolean; tone?: 'default' | 'danger' }>()
const router = useRouter(), element = ref<HTMLDivElement>()
let instance: echarts.ECharts | undefined, observer: ResizeObserver | undefined
let generation = 0, lastWidth = 0
const label = (key: string) => metricNames[key] || categoryNames[key] || key
const hasValues = computed(() => props.chart.available && props.chart.data.some(row => props.comparison
  ? row.left_value != null || row.right_value != null
  : row.value != null))
const height = computed(() => Math.max(260, props.chart.data.length * (props.comparison ? 64 : 38) + 72))
const countChart = computed(() => props.chart.title === '成功执行耗时分布' || props.chart.data.every(row => Boolean(categoryNames[row.name])))
const unit = computed(() => props.comparison ? '均分 / 5' : props.difference ? '分差 · 右−左' : countChart.value ? '用例数' : '')
const sampleScope = (row: ChartDatum) => props.paired === false
  ? `左有效 ${row.left_valid_count ?? row.case_run_ids.length} / 右有效 ${row.right_valid_count ?? row.right_case_run_ids?.length ?? 0} · 各自统计`
  : row.paired_count !== undefined ? `共同有效 ${row.paired_count} 对` : `${row.case_run_ids.length} 条用例`
const chartDescription = computed(() => props.chart.data.map(row => `${label(row.name)}：${props.comparison
  ? `左侧 ${number(row.left_value)}，右侧 ${number(row.right_value)}`
  : number(row.value)}，${sampleScope(row)}${row.reason ? `，${row.reason}` : ''}`).join('；'))

async function render() {
  const current = ++generation
  await nextTick()
  if (current !== generation) return
  instance?.dispose(); instance = undefined
  if (!element.value || !hasValues.value || element.value.clientWidth === 0) return
  const styles = getComputedStyle(element.value)
  const token = (name: string) => styles.getPropertyValue(name).trim()
  const rows = props.chart.data
  const labelWidth = element.value.clientWidth < 420 ? 100 : 136
  const valueLabel = {
    show: true, position: 'right' as const, fontSize: 12, color: token('--ink'),
    formatter: (value: { value?: unknown }) => number(typeof value.value === 'number' ? value.value : null),
  }
  instance = echarts.init(element.value)
  instance.setOption({
    animation: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
    color: [token(props.tone === 'danger' ? '--danger' : '--chart-primary'), token('--chart-secondary')],
    textStyle: { fontFamily: token('--font-ui'), fontSize: 12, color: token('--ink') },
    aria: { enabled: true, description: `${props.chart.title}。${chartDescription.value}` },
    tooltip: { trigger: 'axis', renderMode: 'richText', axisPointer: { type: 'shadow' }, textStyle: { fontSize: 12 } },
    legend: { show: props.comparison, bottom: 0, textStyle: { color: token('--muted'), fontSize: 12 } },
    grid: { top: 16, left: 16, right: 52, bottom: props.comparison ? 64 : 32, containLabel: true },
    xAxis: {
      type: 'value', name: '', minInterval: countChart.value ? 1 : undefined,
      nameTextStyle: { color: token('--muted'), fontSize: 12 },
      axisLabel: { color: token('--muted'), fontSize: 12 },
      splitLine: { lineStyle: { color: token('--line'), opacity: 0.6 } },
    },
    yAxis: {
      type: 'category', inverse: true, data: rows.map(row => label(row.name)),
      axisLabel: { fontSize: 12, color: token('--muted'), width: labelWidth, overflow: 'break', interval: 0, lineHeight: 18 },
      axisLine: { show: false }, axisTick: { show: false },
    },
    series: props.comparison ? [
      { name: props.paired === false ? '左侧有效样本（口径受限）' : '左侧共同样本', type: 'bar', data: rows.map(row => row.left_value ?? null), barMaxWidth: 20, label: valueLabel },
      { name: props.paired === false ? '右侧有效样本（口径受限）' : '右侧共同样本', type: 'bar', data: rows.map(row => row.right_value ?? null), barMaxWidth: 20, label: valueLabel },
    ] : [{ name: props.chart.title, type: 'bar', data: rows.map(row => row.value), barMaxWidth: 24, label: valueLabel }],
  })
  instance.on('click', event => {
    const row = rows[event.dataIndex]
    if (!row) return
    const right = props.comparison && event.seriesIndex === 1
    const link = right ? row.right_link : row.link
    const ids = right ? row.right_case_run_ids : row.case_run_ids
    if (link && ids?.length) void router.push(link)
  })
}
watch(() => [props.chart, props.comparison, props.paired, props.difference, props.tone], render, { deep: true })
onMounted(() => {
  void render()
  observer = new ResizeObserver(entries => {
    const width = entries[0]?.contentRect.width || 0
    if (width === lastWidth) return
    lastWidth = width
    if (width > 0) void render()
  })
  if (element.value) observer.observe(element.value)
})
onBeforeUnmount(() => { generation++; observer?.disconnect(); instance?.dispose() })
</script>

<template>
  <section class="panel chart-panel">
    <div class="panel-heading"><h2>{{ chart.title }}</h2><span class="small muted">{{ unit }} · 点击数据或下方链接查看参与用例</span></div>
    <div v-if="!hasValues" class="chart-empty"><p>当前没有可绘制的有效样本</p><small>{{ chart.reason || '样本为空或指标不可用，不以零分替代。' }}</small></div>
    <div v-show="hasValues" ref="element" class="report-chart" :style="{ height: `${height}px` }" :aria-label="`${chart.title}。${chartDescription}`" role="img" />
    <div v-if="chart.data.length" class="chart-drill" aria-label="图表精确值与参与用例">
      <div v-for="(row, index) in chart.data" :key="index" class="chart-drill-row">
        <div class="chart-drill-labels">
          <RouterLink v-if="row.case_run_ids.length" class="text-link" :to="row.link">{{ label(row.name) }}{{ comparison ? ' · 左侧样本' : '' }}</RouterLink>
          <span v-else>{{ label(row.name) }}</span>
          <RouterLink v-if="comparison && row.right_link && row.right_case_run_ids?.length" class="text-link" :to="row.right_link">右侧样本</RouterLink>
        </div>
        <span class="chart-drill-value">{{ comparison ? `${number(row.left_value)} / ${number(row.right_value)}` : number(row.value) }}</span>
        <small>{{ sampleScope(row) }}<span v-if="row.value === null && row.reason"> · {{ row.reason }}</span></small>
      </div>
    </div>
  </section>
</template>

<style scoped>
.chart-panel { min-width: 0; }
.chart-panel .panel-heading { padding: var(--space-4) var(--space-5); align-items: flex-start; flex-wrap: wrap; gap: var(--space-2); }
.chart-panel .panel-heading h2 { font-size: var(--text-md); }
.chart-panel .panel-heading .small { font-size: var(--text-xs); }
.chart-panel .report-chart { width: 100%; }
.chart-panel .chart-empty { padding: var(--space-6); color: var(--muted); min-height: 260px; }
.chart-panel .chart-empty p { color: var(--ink); }
.chart-panel .chart-empty small { font-size: var(--text-xs); overflow-wrap: anywhere; }
.chart-panel .chart-drill { padding: 0 var(--space-5) var(--space-4); }
.chart-panel .chart-drill-row { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: baseline; gap: var(--space-2) var(--space-3); font-size: var(--text-xs); padding: var(--space-2) 0; border-top: 1px solid var(--line); }
.chart-drill-labels { display: flex; flex-wrap: wrap; gap: var(--space-1) var(--space-3); min-width: 0; overflow-wrap: anywhere; }
.chart-panel .chart-drill-value { font-family: var(--font-data); font-size: var(--text-sm); font-variant-numeric: tabular-nums; color: var(--ink); margin-left: 0; }
.chart-panel .chart-drill small { font-size: var(--text-xs); color: var(--muted); min-width: 0; overflow-wrap: anywhere; }
@media(max-width: 480px) { .chart-panel .chart-drill-row { grid-template-columns: minmax(0, 1fr) auto; } .chart-panel .chart-drill-row > small { grid-column: 1 / -1; text-align: left; } }
</style>

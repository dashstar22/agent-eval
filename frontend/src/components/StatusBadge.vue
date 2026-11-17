<script setup lang="ts">
import { computed } from 'vue'
import { stateNames } from '../workbench'

const props = withDefaults(defineProps<{
  value?: string | null
  domain?: 'quality' | 'execution' | 'scoring' | 'run' | 'verification' | 'neutral'
}>(), { domain: 'neutral' })

const label = computed(() => {
  const value = props.value || (props.domain === 'quality' ? 'UNDETERMINED' : 'PENDING')
  if (props.domain === 'execution' && value === 'FAILED') return '执行失败'
  if (props.domain === 'execution' && value === 'PROTOCOL_ERROR') return '协议错误'
  if (props.domain === 'execution' && value === 'RESULT_UNKNOWN') return '执行结果未知'
  if (props.domain === 'scoring') {
    if (value === 'SUCCEEDED' || value === 'COMPLETED') return '评分完成'
    if (value === 'FAILED' || value === 'ERROR') return '评分故障'
    if (value === 'RUNNING') return '评分中'
  }
  return stateNames[value] || value
})
const tone = computed(() => {
  const value = props.value || ''
  if (props.domain === 'quality') {
    if (value === 'PASS') return 'success'
    if (value === 'FAIL') return 'danger'
    return 'warning'
  }
  if (['FAILED', 'ERROR', 'TIMEOUT', 'PROTOCOL_ERROR'].includes(value)) return 'danger'
  if (['INTERRUPTED', 'UNDETERMINED', 'NA', 'RESULT_UNKNOWN'].includes(value)) return 'warning'
  if (['RUNNING', 'QUEUED'].includes(value)) return 'info'
  return 'neutral'
})
</script>

<template>
  <span class="status-badge" :class="`status-badge--${tone}`">
    <span v-if="domain === 'quality'" aria-hidden="true">{{ value === 'PASS' ? '✓' : value === 'FAIL' ? '×' : '!' }}</span>
    {{ label }}
  </span>
</template>

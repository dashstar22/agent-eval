<script setup lang="ts">
import type { ApiProblem } from '../types'
defineProps<{ loading?: boolean; error?: ApiProblem | null; empty?: boolean; emptyTitle?: string; emptyText?: string }>()
defineEmits<{ retry: [] }>()
</script>
<template>
  <div v-if="loading" class="state-panel" role="status"><span class="loading-mark"></span>正在读取已保存的数据…</div>
  <div v-else-if="error" class="state-panel error-panel" role="alert"><strong>{{ error.message }}</strong><span class="mono">{{ error.code }}</span><small v-if="error.trace_id">追踪编号：{{ error.trace_id }}</small><ElButton @click="$emit('retry')">重新加载</ElButton></div>
  <div v-else-if="empty" class="state-panel"><div class="empty-mark" aria-hidden="true">▤</div><strong>{{ emptyTitle || '这里还没有数据' }}</strong><p>{{ emptyText || '创建第一条记录，开始维护评测输入。' }}</p><slot name="empty-action" /></div>
  <slot v-else />
</template>

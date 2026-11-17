<script setup lang="ts">
import type { VersionVerification } from '../types'
import { pretty } from '../workbench'
defineProps<{ value: VersionVerification }>()
</script>
<template><section class="version-panel"><h3>实际版本核验 <ElTag size="small" type="info">{{ value.status }}</ElTag></h3><p class="small muted">候选标签：{{ value.candidate_label || '未填写' }}。标签不证明已修改实例配置；模型别名不等于不可变模型快照。</p><div class="table-scroll"><table><thead><tr><th>版本维度</th><th>预期</th><th>实际观测</th><th>核验与覆盖</th></tr></thead><tbody><tr v-for="item in value.dimensions" :key="item.dimension"><td>{{ item.dimension }}</td><td class="mono small">{{ pretty(item.expected) }}</td><td class="mono small">{{ pretty(item.observed) }}</td><td>{{ item.status }} · {{ item.verification }}<div class="table-subtext">预期匹配：{{ item.expected_match === null ? '未知' : item.expected_match ? '一致' : '不一致' }} · 已核验 {{ item.verified_count }}/{{ item.total_count }} · {{ item.reason }}</div></td></tr></tbody></table></div><ul v-if="value.limitations.length" class="limitation-list"><li v-for="reason in value.limitations" :key="reason">{{ reason }}</li></ul></section></template>

<script setup lang="ts">
import { useRoute } from 'vue-router'
const route = useRoute()
</script>
<template><div class="page-heading"><div><div class="eyebrow">AGENTEVAL WORKSPACE</div><h1>{{ route.meta.title }}</h1><p>{{ route.meta.caption }}</p></div><ElTag type="info" effect="plain">待实现</ElTag></div><section class="panel state-panel pending-view"><div class="empty-mark" aria-hidden="true">◫</div><h2>此页面尚未实现</h2><p>当前可维护数据集和 Agent 配置。此入口暂不展示运行、评分或报告数据。</p><RouterLink to="/datasets"><ElButton type="primary">前往数据集</ElButton></RouterLink></section></template>

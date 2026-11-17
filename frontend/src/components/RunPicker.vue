<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { api, problem } from '../api'
import { allPages, date, stateNames, sourceNames } from '../workbench'
import type { ApiProblem, Run, ScoreSet } from '../types'
import ProblemBox from './ProblemBox.vue'
const props = defineProps<{ runId: string; scoreSetId?: string; label?: string; allowEmpty?: boolean }>()
const emit = defineEmits<{ change: [run: string, scoreSet: string] }>()
const runs = ref<Run[]>([]), sets = ref<ScoreSet[]>([]), loading = ref(true), error = ref<ApiProblem | null>(null)
let request = 0
async function loadSets(runId: string) { const current = ++request; sets.value = []; if (!runId) return; try { const data = await allPages(page => api.scoreSets(runId, page)); if (current === request) sets.value = data } catch (e) { if (current === request) error.value = problem(e) } }
async function load() { loading.value = true; error.value = null; try { runs.value = await allPages(page => api.runs(page)); await loadSets(props.runId) } catch (e) { error.value = problem(e) } finally { loading.value = false } }
async function selectRun(event: Event) { const id = (event.target as HTMLSelectElement).value; emit('change', id, ''); error.value = null; await loadSets(id) }
onMounted(load)
watch(() => [props.runId, props.scoreSetId], () => { error.value = null; void loadSets(props.runId) })
</script>
<template><div class="run-picker"><label>{{ label || '运行' }}<select :value="runId" :disabled="loading" @change="selectRun"><option value="">{{ allowEmpty ? '不选择对照运行' : loading ? '正在加载运行…' : '请选择运行' }}</option><option v-for="run in runs" :key="run.id" :value="run.id">{{ date(run.created_at) }} · {{ stateNames[run.status] || run.status }} · {{ sourceNames[run.source_type] || run.source_type }} · {{ run.id.slice(0, 8) }}</option></select></label><label v-if="runId">评分集<select :value="scoreSetId || ''" @change="emit('change', runId, ($event.target as HTMLSelectElement).value)"><option value="">ORIGINAL（默认原评分）</option><option v-for="item in sets" :key="item.id" :value="item.id">{{ item.kind }} · {{ item.status }} · {{ item.id.slice(0, 8) }}</option></select></label><ProblemBox :error="error" /><button type="button" class="text-link small" :disabled="loading" @click="load">{{ error ? '重新读取运行' : '刷新运行列表' }}</button></div></template>

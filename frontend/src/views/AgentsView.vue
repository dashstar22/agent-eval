<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { api, problem, validation } from '../api'
import type { AdapterType, AgentConfig, AgentInput, ApiProblem, SourceType } from '../types'
import AsyncState from '../components/AsyncState.vue'
import ProblemBox from '../components/ProblemBox.vue'
import FormModal from '../components/FormModal.vue'

const items = ref<AgentConfig[]>([]), total = ref(0), page = ref(1), loading = ref(true), loadError = ref<ApiProblem | null>(null)
const actionError = ref<ApiProblem | null>(null), formError = ref<ApiProblem | null>(null), notice = ref(''), modal = ref(false), saving = ref(false), busy = ref(false), editId = ref<number>()
const versionFields = [['build_sha256', '构建 SHA-256'], ['prompt_sha256', 'Prompt SHA-256'], ['model_id', '模型标识'], ['knowledge_version', '知识版本'], ['version_label', '候选版本标签']]
const sourceLabels: Record<SourceType, string> = { MOCK: 'Mock 模拟', PROTOCOL_DOUBLE: '协议替身', REAL_EXTERNAL: '真实外部连接' }
const form = reactive({ name: '', adapter_type: 'MOCK' as AdapterType, base_url: 'http://127.0.0.1', timeout_ms: '195000', username_ref: '', password_ref: '', bearer_ref: '', expected_version: {} as Record<string, string>, source_type: 'MOCK' as SourceType, enabled: true })
let generation = 0
async function load() {
  const current = ++generation; loading.value = true; loadError.value = null
  try { const data = await api.agents(page.value); if (current === generation) { items.value = data.items; total.value = data.total } }
  catch (error) { if (current === generation) loadError.value = problem(error) }
  finally { if (current === generation) loading.value = false }
}
onMounted(load)
function open(row?: AgentConfig) {
  formError.value = null; notice.value = ''; editId.value = row?.id
  Object.assign(form, { name: row?.name || '', adapter_type: row?.adapter_type || 'MOCK', base_url: row?.base_url || 'http://127.0.0.1', timeout_ms: String(row?.timeout_ms || 195000), username_ref: row?.credential_refs.username_ref || '', password_ref: row?.credential_refs.password_ref || '', bearer_ref: row?.credential_refs.bearer_ref || '', expected_version: { ...(row?.expected_version || {}) }, source_type: row?.source_type || 'MOCK', enabled: row?.enabled ?? true })
  modal.value = true
}
function protocolChanged() { form.source_type = form.adapter_type === 'MOCK' ? 'MOCK' : 'PROTOCOL_DOUBLE' }
async function save() {
  if (saving.value) return
  const errors: { field: string; message: string }[] = []
  if (!form.name.trim()) errors.push({ field: 'name', message: '请输入连接名称。' })
  try { const url = new URL(form.base_url); if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || !url.hostname) throw new Error() }
  catch { errors.push({ field: 'base_url', message: '请输入完整 HTTP(S) 地址，不含账号、密码、查询参数或片段。' }) }
  const timeout = Number(form.timeout_ms)
  if (!Number.isInteger(timeout) || timeout < 1 || timeout > 360000) errors.push({ field: 'timeout_ms', message: '超时应为 1—360000 毫秒的整数。' })
  for (const key of ['username_ref', 'password_ref', 'bearer_ref'] as const) {
    if (form[key] && !/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(form[key])) errors.push({ field: `credential_refs.${key}`, message: '填写环境变量名；以字母或下划线开头，仅含字母、数字、下划线。' })
  }
  if (form.adapter_type === 'PRINTER_HTTP') for (const key of ['username_ref', 'password_ref'] as const) if (!form[key]) errors.push({ field: `credential_refs.${key}`, message: 'PrinterHttp 连接必须填写此环境引用。' })
  formError.value = validation(errors)
  if (formError.value) return
  const input: AgentInput = { name: form.name.trim(), adapter_type: form.adapter_type, base_url: form.base_url.trim(), timeout_ms: timeout, credential_refs: { username_ref: form.username_ref || null, password_ref: form.password_ref || null, bearer_ref: form.bearer_ref || null }, expected_version: Object.fromEntries(Object.entries(form.expected_version).filter(([, value]) => value.trim()).map(([key, value]) => [key, value.trim()])), enabled: form.enabled, source_type: form.adapter_type === 'MOCK' ? 'MOCK' : form.source_type }
  saving.value = true
  try { await api.saveAgent(input, editId.value); modal.value = false; notice.value = 'Agent 配置已保存'; await load() }
  catch (error) { formError.value = problem(error) }
  finally { saving.value = false }
}
async function toggle(row: AgentConfig) {
  busy.value = true; actionError.value = null; notice.value = ''
  try { const { id: _id, revision: _revision, credential_status: _status, created_at: _created, updated_at: _updated, ...input } = row; await api.saveAgent({ name: input.name, adapter_type: input.adapter_type, base_url: input.base_url, timeout_ms: input.timeout_ms, credential_refs: input.credential_refs, expected_version: input.expected_version, enabled: !row.enabled, source_type: input.source_type }, row.id); notice.value = row.enabled ? 'Agent 配置已停用' : 'Agent 配置已启用'; await load() }
  catch (error) { actionError.value = problem(error) }
  finally { busy.value = false }
}
async function remove(row: AgentConfig) {
  if (!window.confirm(`删除 Agent 配置“${row.name}”？历史运行中的配置快照仍会保留。`)) return
  busy.value = true; actionError.value = null; notice.value = ''
  try { await api.deleteAgent(row.id); if (items.value.length === 1 && page.value > 1) page.value--; notice.value = 'Agent 配置已删除'; await load() }
  catch (error) { actionError.value = problem(error) }
  finally { busy.value = false }
}
</script>

<template>
  <div class="page-heading"><div><div class="eyebrow">AGENT CONNECTIONS</div><h1>Agent 配置</h1><p>维护被测 Agent 的连接、超时和候选版本，凭证使用本机环境引用。</p></div><ElButton type="primary" @click="open()">＋ 新建 Agent 配置</ElButton></div>
  <div v-if="notice" class="notice" role="status">✓ {{ notice }}</div><ProblemBox :error="actionError" />
  <div class="connection-note"><span class="note-icon" aria-hidden="true">⌘</span><div><strong>连接声明与实际执行证据分别保存</strong><p>Mock 和协议替身会保留来源标记；填写候选版本不代表已核验实例的生效版本。</p></div></div>
  <section class="panel"><div class="panel-heading"><h2>连接配置 <span class="count-label">{{ total }}</span></h2><ElButton :disabled="loading" @click="load">刷新配置</ElButton></div><AsyncState :loading="loading" :error="loadError" :empty="!items.length" empty-title="还没有 Agent 连接" empty-text="先创建 Mock 开发连接，或填写隔离服务的真实连接信息。" @retry="load"><template #empty-action><ElButton type="primary" @click="open()">新建 Agent 配置</ElButton></template><div class="table-scroll"><table><thead><tr><th>名称 / 连接地址</th><th>协议与来源</th><th>凭证引用</th><th>超时 / 状态</th><th class="actions-column">操作</th></tr></thead><tbody><tr v-for="row in items" :key="row.id"><td><button class="text-link record-title" :aria-label="`查看 Agent 配置 ${row.name}`" @click="open(row)">{{ row.name }}</button><div class="table-subtext mono">{{ row.base_url }}</div><details v-if="Object.keys(row.expected_version).length" class="inline-detail"><summary>候选版本</summary><dl class="version-list"><template v-for="(value, key) in row.expected_version" :key="key"><dt>{{ key }}</dt><dd class="mono">{{ value }}</dd></template></dl></details></td><td><span class="mono small">{{ row.adapter_type }}</span><div><ElTag size="small" :type="row.source_type === 'REAL_EXTERNAL' ? 'primary' : 'warning'" effect="plain">{{ sourceLabels[row.source_type] }}</ElTag></div></td><td><div v-for="(value, key) in row.credential_refs" :key="key" class="credential-row"><template v-if="value"><span class="mono small">{{ value }}</span><span :class="row.credential_status[key] === 'RESOLVED' ? 'resolved' : 'unresolved'">{{ row.credential_status[key] === 'RESOLVED' ? '已解析' : '未解析' }}</span></template></div><span v-if="!Object.values(row.credential_refs).some(Boolean)" class="muted">无需凭证引用</span></td><td><div class="mono small">{{ row.timeout_ms }} ms</div><ElTag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '已启用' : '已停用' }}</ElTag></td><td><div class="row-actions"><ElButton size="small" :aria-label="`编辑 Agent 配置 ${row.name}`" :disabled="busy" @click="open(row)">编辑</ElButton><ElButton size="small" :aria-label="`${row.enabled ? '停用' : '启用'} Agent 配置 ${row.name}`" :disabled="busy" @click="toggle(row)">{{ row.enabled ? '停用' : '启用' }}</ElButton><ElButton size="small" type="danger" plain :aria-label="`删除 Agent 配置 ${row.name}`" :disabled="busy" @click="remove(row)">删除</ElButton></div></td></tr></tbody></table></div><div v-if="total > 20" class="pagination"><ElPagination v-model:current-page="page" :page-size="20" :total="total" layout="prev, pager, next" @current-change="load" /></div></AsyncState></section>
  <FormModal v-if="modal" :title="editId ? '编辑 Agent 配置' : '新建 Agent 配置'" :saving="saving" wide @close="!saving && (modal = false)"><form class="editor-form" novalidate @submit.prevent="save"><ProblemBox :error="formError" /><div class="form-grid"><label>连接名称 <span class="required">*</span><input v-model="form.name" aria-label="连接名称" maxlength="200" placeholder="例如：隔离售后候选 A" /></label><label class="check-field"><input v-model="form.enabled" type="checkbox" aria-label="启用 Agent 配置" /> 启用此配置</label></div><div class="form-grid"><label>连接协议<select v-model="form.adapter_type" aria-label="连接协议" @change="protocolChanged"><option value="MOCK">Mock 开发模拟</option><option value="HTTP">通用 HTTP</option><option value="PRINTER_HTTP">PrinterHttp 售后连接</option></select></label><label>来源声明<select v-model="form.source_type" aria-label="来源声明" :disabled="form.adapter_type === 'MOCK'"><option v-if="form.adapter_type === 'MOCK'" value="MOCK">Mock 模拟</option><template v-else><option value="PROTOCOL_DOUBLE">协议替身</option><option value="REAL_EXTERNAL">真实外部连接</option></template></select></label></div><label>连接地址 <span class="required">*</span><input v-model="form.base_url" aria-label="连接地址" maxlength="2048" placeholder="http://127.0.0.1:18090" class="mono" /><small>完整 HTTP(S) 地址，不在 URL 中填写凭证或令牌。</small></label><label>请求超时（毫秒） <span class="required">*</span><input v-model="form.timeout_ms" aria-label="请求超时（毫秒）" inputmode="numeric" /><small>1—360000 毫秒。运行的用例截止与批次预算由后端统一限制。</small></label><fieldset><legend>凭证环境引用</legend><p class="field-help">仅填写环境变量名称。凭证值在后端进程环境提供，保存不会发起连接请求。</p><div class="form-grid"><label>账号环境引用<input v-model="form.username_ref" aria-label="账号环境引用" class="mono" maxlength="128" placeholder="PRINTER_USERNAME" /></label><label>密码环境引用<input v-model="form.password_ref" aria-label="密码环境引用" class="mono" maxlength="128" placeholder="PRINTER_PASSWORD" /></label></div><label>Bearer 环境引用<input v-model="form.bearer_ref" aria-label="Bearer 环境引用" class="mono" maxlength="128" placeholder="AGENT_BEARER_TOKEN" /></label><small v-if="form.adapter_type === 'PRINTER_HTTP'">PrinterHttp 必须提供账号和密码的环境引用。</small></fieldset><details class="advanced-fields"><summary>候选版本信息</summary><p class="field-help">用于后续与实际生效版本核对。未知字段可留空。</p><div class="form-grid"><label v-for="entry in versionFields" :key="entry[0]">{{ entry[1] }}<input v-model="form.expected_version[entry[0]]" :aria-label="entry[1]" maxlength="500" class="mono" /></label></div></details><div class="form-actions"><ElButton :disabled="saving" @click="modal = false">取消</ElButton><ElButton type="primary" native-type="submit" :loading="saving">保存 Agent 配置</ElButton></div></form></FormModal>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { call, size } from './api'
import VirtualList from './VirtualList.vue'
const props = defineProps({ state: { type: Object, default: () => ({}) }, boot: Object })
const emit = defineEmits(['refresh', 'error'])
const query = ref(''), orphans = ref(false), list = ref(), detail = ref(null), busy = ref(false), message = ref(''), preview = ref(null), confirmed = ref(false), relationSearch = ref('')
const formOpen = ref(false), name = ref(''), path = ref(''), isDependency = ref(false), known = ref(true), boundary = ref(false), dependencyIds = ref([]), relationQuery = ref(''), relationOptions = ref([])
const load = page => call('deps_rows', { ...page, query: query.value, orphans: orphans.value })
const adjacent = computed(() => ({
  dependencies: (detail.value?.dependencies || []).filter(row => row.name.toLowerCase().includes(relationSearch.value.toLowerCase())).slice(0, 200),
  reverse: (detail.value?.reverse_dependencies || []).filter(row => row.name.toLowerCase().includes(relationSearch.value.toLowerCase())).slice(0, 200),
}))
function sourceName(row) { return ({ APT: 'APT 软件包', AppX: '当前用户 AppX', Desktop: '桌面软件', Portable: '登记便携目录' })[row.source] || row.source }
function status(row) {
  if (row.source === 'Portable' && row.present === false) return '目录已移走 / 已回收'
  if (row.orphan) return '孤儿候选'
  if (row.keep) return '手动保留'
  if (row.dependency_status === 'unknown') return '依赖未知'
  if (row.source === 'APT') return row.auto ? '自动安装' : '手动安装'
  if (row.source === 'Portable') return row.is_dependency ? '登记依赖' : '独立应用'
  return '已知声明依赖'
}
async function perform(action) {
  busy.value = true
  try { await action(); emit('refresh'); list.value?.reload(false) }
  catch (error) { emit('error', error.message) }
  finally { busy.value = false }
}
async function scan() { await perform(() => call('deps_scan')) }
async function show(id) { await perform(async () => { detail.value = await call('deps_details', { id }); relationSearch.value = '' }) }
async function plan() {
  await perform(async () => {
    const portable = detail.value.source === 'Portable'
    const value = await call(portable ? 'portable_preview' : 'deps_preview', portable ? { id: detail.value.id } : { ids: [detail.value.id] })
    preview.value = { ...value, portable }; confirmed.value = false
  })
}
async function execute() {
  if (!confirmed.value || !preview.value) return
  await perform(async () => {
    const value = preview.value
    const result = await call(value.portable ? 'vault_recycle' : 'deps_uninstall', { token: value.token, confirmed: true })
    message.value = result.error || result.message || (value.portable ? '目录已移入应用回收站，可在文件页查看与恢复。' : '正式卸载入口已返回，请核对登记状态。')
    preview.value = null
    if (detail.value) {
      try { detail.value = await call('deps_details', { id: detail.value.id }) }
      catch { detail.value = null }
    }
  })
}
async function keep() { await perform(async () => { await call('portable_keep', { id: detail.value.id, keep: !detail.value.keep }); detail.value = await call('deps_details', { id: detail.value.id }) }) }
async function chooseFolder() {
  await perform(async () => { const value = await call('pick_folder'); if (value) path.value = typeof value === 'string' ? value : value.path })
}
let relationRequest = 0
async function searchRelations() {
  const request = ++relationRequest
  try { const value = await call('deps_rows', { query: relationQuery.value, limit: 100 }); if (request === relationRequest) relationOptions.value = value.rows }
  catch (error) { emit('error', error.message) }
}
function edit() {
  const row = detail.value
  name.value = row.name; path.value = row.path; isDependency.value = !!row.is_dependency
  known.value = row.dependency_status === 'known'; dependencyIds.value = row.dependencies.map(item => item.id)
  boundary.value = false; formOpen.value = true; searchRelations()
}
async function register() {
  await perform(async () => {
    const row = await call('portable_register', { path: path.value, name: name.value, dependencies: known.value ? dependencyIds.value : null, is_dependency: isDependency.value })
    detail.value = await call('deps_details', { id: row.id }); formOpen.value = false; boundary.value = false
    message.value = '便携目录与依赖关系已保存；登记相同目录会更新现有关系。'
  })
}
watch(relationQuery, searchRelations)
watch(formOpen, value => { if (value) searchRelations() })
watch(() => props.state.generation, () => { list.value?.reload(false) })
</script>

<template>
  <section class="panel dependency-panel">
    <div class="section-heading"><div><h2>应用依赖与孤儿候选</h2><p class="muted">{{ boot?.platform === 'Windows' ? '查看 AppX 声明依赖与桌面软件登记。' : '查看已安装 APT 软件包及包管理器确认的孤儿候选。' }}</p></div><button :disabled="busy || state.busy" @click="scan">{{ state.busy ? '正在读取…' : '扫描依赖' }}</button></div>
    <div class="toolbar dependency-toolbar"><input v-model="query" aria-label="搜索依赖应用" placeholder="搜索应用或包名称"><label><input v-model="orphans" type="checkbox">只看孤儿候选（{{ state.orphans || 0 }}）</label><button @click="formOpen = !formOpen">登记便携目录</button></div>
    <div class="dependency-body">
      <VirtualList ref="list" :load="load" :revision="query + '|' + orphans" row-key="id" empty="点击扫描依赖，或登记独立的便携应用目录" @error="emit('error', $event)">
        <template #default="{ row }"><button class="dependency-row" :class="{ active: detail?.id === row.id }" @click="show(row.id)"><span class="truncate"><b>{{ row.name }}</b><small class="muted">{{ sourceName(row) }} · {{ row.version || '本地登记' }}</small></span><small>{{ status(row) }}</small></button></template>
      </VirtualList>
      <div v-if="detail" class="dependency-detail">
        <div class="section-heading"><h3 class="truncate" :title="detail.name">{{ detail.name }}</h3><button aria-label="关闭依赖详情" @click="detail = null">×</button></div>
        <p class="muted">{{ sourceName(detail) }} · {{ status(detail) }}</p><p v-if="detail.path" class="dependency-path">{{ detail.path }}</p>
        <p v-if="detail.orphan_reason">{{ detail.orphan_reason }}</p>
        <p v-if="detail.dependency_status === 'unknown'" class="muted">没有已知依赖元数据，不能据此判定孤儿。</p>
        <input v-model="relationSearch" placeholder="筛选相邻依赖名称（每侧最多显示 200 项）" aria-label="筛选相邻依赖">
        <h3>依赖（{{ detail.dependencies.length }}）</h3>
        <div class="dependency-links"><button v-for="row in adjacent.dependencies" :key="row.id" @click="show(row.id)">{{ row.name }}</button><p v-if="!detail.dependencies.length" class="muted">{{ detail.dependency_status === 'unknown' ? '未知' : '无已知依赖' }}</p></div>
        <h3>反向依赖（{{ detail.reverse_dependencies.length }}）</h3>
        <div class="dependency-links"><button v-for="row in adjacent.reverse" :key="row.id" @click="show(row.id)">{{ row.name }}</button><p v-if="!detail.reverse_dependencies.length" class="muted">无已知引用；这本身不证明可以清理。</p></div>
        <div class="row wrap gap"><template v-if="detail.source === 'Portable'"><button :disabled="busy" @click="edit">编辑关系</button><button :disabled="busy" @click="keep">{{ detail.keep ? '取消手动保留' : '手动保留' }}</button><button :disabled="busy || detail.present === false" @click="plan">预览目录回收</button></template><button v-else :disabled="busy || !detail.uninstall_available" @click="plan">预览正式卸载</button></div>
      </div>
    </div>
    <form v-if="formOpen" class="portable-form" @submit.prevent="register">
      <h3>登记 / 更新便携应用</h3><p class="muted">必须是您确认的单个应用独立目录。相同目录会更新名称及关系；不从单个 exe 推断目录边界。</p>
      <label>名称<input v-model="name" required placeholder="便携应用名称"></label><label>完整目录<div class="row gap"><input v-model="path" required placeholder="选择独立目录"><button type="button" @click="chooseFolder">选择目录</button></div></label>
      <label><input v-model="isDependency" type="checkbox">这是供其他应用使用的依赖（独立应用默认手动保留）</label>
      <label><input v-model="known" type="checkbox">我已明确登记依赖关系（不选表示未知）</label>
      <template v-if="known"><p class="muted">下面勾选本应用依赖的已登记项目；不勾选表示明确无依赖。可搜索名称，选中项跨搜索保留（{{ dependencyIds.length }}）。</p><input v-model="relationQuery" aria-label="搜索可登记依赖" placeholder="搜索应用或包名称"><div class="portable-relations"><label v-for="row in relationOptions" :key="row.id"><input v-model="dependencyIds" type="checkbox" :value="row.id">{{ row.name }} · {{ sourceName(row) }}</label></div><button v-if="dependencyIds.length" type="button" @click="dependencyIds = []">清空已选依赖</button></template>
      <label><input v-model="boundary" type="checkbox" required>我确认目录仅属于此便携应用，且已核对依赖关系。</label>
      <div class="row gap"><button class="primary" :disabled="busy || !boundary">保存登记</button><button type="button" @click="formOpen = false">取消</button></div>
    </form>
    <p v-if="message" role="status">{{ message }}</p><p v-if="state.error" class="error-text">{{ state.error }}</p><details v-if="state.errors?.length"><summary>依赖扫描提示</summary><p v-for="error in state.errors" :key="error">{{ error }}</p></details>
    <details class="quiet-details"><summary>识别依据与操作说明</summary><p>扫描与零反向引用均不授权清理。手动保留、未知关系、系统组件与 AppX 框架不会自动推荐卸载。便携关系未知时不推荐便携孤儿。</p><p>包管理器卸载不进入回收站。目录回收不直接释放同卷空间，需在应用回收站另行确认永久删除。</p></details>
    <Teleport to="body"><div v-if="preview" class="modal-backdrop" @keydown.esc="!busy && (preview = null)"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="dependency-confirm-title"><h2 id="dependency-confirm-title">{{ preview.portable ? '确认便携目录回收' : '确认正式卸载' }}</h2><p>{{ preview.message }}</p>
      <p v-if="preview.permission" class="muted">{{ preview.permission }}</p><p v-if="preview.space_bytes_estimate != null">已安装逻辑大小估算：{{ size(preview.space_bytes_estimate) }}；实际空间变化由包管理器决定。</p>
      <div class="preview-files"><template v-if="preview.portable"><div v-for="file in preview.files" :key="file.path" class="preview-file"><span>{{ file.path }}</span><span>{{ size(file.bytes) }}</span></div></template><template v-else><div v-for="row in preview.affected" :key="row.id" class="preview-file"><span>{{ row.name }}</span><span>{{ row.version }}</span></div></template></div>
      <details v-if="preview.notes?.length"><summary>目录内容风险说明</summary><p v-for="note in preview.notes" :key="note.name">{{ note.name }} · {{ note.risk }} · {{ note.reason }}</p></details>
      <label class="medium-confirm"><input v-model="confirmed" type="checkbox">{{ preview.portable ? '我已核对具体目录，同意移入应用回收站。' : '我已核对全部受影响应用，理解正式卸载无法从应用回收站恢复。' }}</label><div class="modal-actions"><button :disabled="busy" @click="preview = null">取消</button><button class="danger" :disabled="!confirmed || busy" @click="execute">{{ busy ? '正在处理…' : preview.portable ? '确认回收目录' : '确认正式卸载' }}</button></div>
    </section></div></Teleport>
  </section>
</template>

<style scoped>
.dependency-panel{padding:18px;display:flex;flex-direction:column;gap:14px;min-height:440px}.dependency-panel p{font-size:12px}.dependency-toolbar{flex-wrap:wrap}.dependency-toolbar label{display:flex;align-items:center;gap:7px;font-size:12px}.dependency-body{display:flex;gap:16px;min-height:340px}.dependency-body>.virtual-list{flex:1;min-width:200px;height:360px}.dependency-row{border:0;background:transparent;border-radius:0;width:100%;height:56px;display:flex;align-items:center;justify-content:space-between;gap:12px;text-align:left;border-bottom:1px solid var(--line);padding:7px 10px}.dependency-row>span{display:flex;flex-direction:column;gap:4px}.dependency-row>b{font-size:13px}.dependency-detail{flex:1;min-width:0;display:flex;flex-direction:column;gap:12px;max-height:570px;overflow:auto;padding:0 4px}.dependency-path{overflow-wrap:anywhere}.dependency-links{display:flex;gap:6px;flex-wrap:wrap;max-height:170px;overflow:auto}.dependency-links button{font-size:12px;padding:5px 8px;max-width:100%;overflow:hidden;text-overflow:ellipsis}.portable-form{display:flex;flex-direction:column;gap:12px;border-top:1px solid var(--line);padding-top:16px}.portable-form>label{font-size:13px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}.portable-form>label>input:not([type=checkbox]),.portable-form>label>div{flex:1;min-width:150px}.portable-form .row input{flex:1}.portable-relations{max-height:180px;overflow:auto;display:flex;flex-direction:column;gap:8px;font-size:12px}.portable-relations label{display:flex;align-items:center;gap:7px}.preview-file>span:first-child{overflow-wrap:anywhere}@media(max-width:900px){.dependency-body{flex-direction:column}.dependency-body>.virtual-list{flex:auto}.dependency-panel>.section-heading{align-items:flex-start}}
</style>

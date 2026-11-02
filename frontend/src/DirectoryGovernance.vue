<script setup>
import { computed, ref, watch } from 'vue'
import { call, size, riskName } from './api'
import VirtualList from './VirtualList.vue'
const props = defineProps({ state: { type: Object, default: () => ({}) }, boot: Object, mode: { type: String, default: 'quota' } })
const emit = defineEmits(['refresh', 'error'])
const selectedRoot = ref(''), chosen = ref(new Set()), vaultChosen = ref(new Set()), folder = ref(''), threshold = ref(10), working = ref(false)
const fileList = ref(), vaultList = ref()
const preview = ref(null), confirmed = ref(false), permanent = ref(null), restoring = ref(null), destination = ref(''), showHistory = ref(false), notice = ref('')
const roots = computed(() => props.state.roots || [])
const root = computed(() => roots.value.find(item => item.id === selectedRoot.value))
const vault = computed(() => (props.state.vault || []).filter(item => showHistory.value || ['vaulted', 'attention'].includes(item.status)))
const vaultRevision = computed(() => String(showHistory.value))
const revision = computed(() => selectedRoot.value)
const status = value => ({ vaulted: '待处理', restored: '已恢复', purged: '已永久删除', failed: '回收失败', attention: '需核对', moving: '移动中', restoring: '恢复中', purging: '删除中' }[value] || value)
const loadRows = page => selectedRoot.value ? call('directory_rows', { id: selectedRoot.value, ...page }) : Promise.resolve({ total: 0, rows: [] })
const loadVault = ({ offset, limit }) => ({ total: vault.value.length, rows: vault.value.slice(offset, offset + limit) })
watch(roots, items => { if (!items.some(item => item.id === selectedRoot.value)) selectedRoot.value = items[0]?.id || '' }, { immediate: true })
watch(() => root.value?.generation, () => fileList.value?.reload(false))
watch(() => props.state.generation, () => vaultList.value?.reload(false))
watch([selectedRoot, () => props.mode], () => { chosen.value = new Set(); vaultChosen.value = new Set() })
function toggle(selection, id) { selection.has(id) ? selection.delete(id) : selection.add(id) }
async function run(task) {
  working.value = true
  try { await task(); emit('refresh') } catch (error) { emit('error', error.message) }
  finally { working.value = false }
}
async function pickFolder() {
  await run(async () => { const result = await call('pick_folder'); folder.value = typeof result === 'string' ? result : result?.path || '' })
}
async function add() {
  await run(async () => { const result = await call('directory_add', { path: folder.value, limit_bytes: Math.round(Number(threshold.value) * 1024 ** 3) }); selectedRoot.value = result.id; folder.value = '' })
}
async function remove(id) { await run(() => call('directory_remove', { id })) }
function showPreview(value) { preview.value = value; confirmed.value = false }
defineExpose({ showPreview })
async function makePreview() { await run(async () => showPreview(await call('vault_preview', { paths: [...chosen.value], source: 'quota' }))) }
async function recycle() { await run(async () => { const result = await call('vault_recycle', { token: preview.value.token, confirmed: true }); notice.value = result.message; if (result.error) emit('error', result.error); preview.value = null; chosen.value = new Set() }) }
function askPurge() { permanent.value = vault.value.filter(item => item.status === 'vaulted' && vaultChosen.value.has(item.id)); confirmed.value = false }
async function purge() { await run(async () => { await call('vault_purge', { ids: permanent.value.map(item => item.id), confirmed: true }); permanent.value = null; vaultChosen.value = new Set() }) }
function askRestore(item) { restoring.value = item; destination.value = item.original; confirmed.value = false }
async function restore() { await run(async () => { await call('vault_restore', { id: restoring.value.id, destination: destination.value, confirmed: true }); restoring.value = null }) }
</script>

<template>
  <section class="panel directory-governance">
    <div class="section-heading"><h2>{{ mode === 'vault' ? '应用回收站' : '目录配额' }}</h2><span v-if="working" class="muted" role="status">正在核验和处理…</span></div>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p class="inline-note">{{ mode === 'vault' ? '仅管理本应用的回收记录。移入回收站不会释放磁盘空间；永久删除需要单独确认。' : '这里的配额是监测阈值，不会阻止写入。超限仅提示，不会自动回收。统计普通文件逻辑大小。' }}</p>
    <template v-if="mode === 'quota'">
      <div class="toolbar"><button @click="pickFolder" :disabled="working">选择目录</button><span class="truncate directory-path" :title="folder">{{ folder || '请选择独立目录' }}</span><label>阈值 <input v-model="threshold" type="number" min="0.001" step="0.5" aria-label="目录阈值 GiB"> GiB</label><button class="primary" @click="add" :disabled="working || !folder || !(Number(threshold) > 0)">添加监测</button></div>
      <div class="directory-roots" aria-label="受监测目录">
        <article v-for="item in roots" :key="item.id" class="directory-root" :class="{ selected: selectedRoot === item.id }">
          <button class="directory-root-select" @click="selectedRoot = item.id"><b class="truncate" :title="item.path">{{ item.path }}</b><span>{{ size(item.bytes) }} / {{ size(item.limit_bytes) }}<strong v-if="item.over_bytes > 0" class="risk high"> · 超出 {{ size(item.over_bytes) }}</strong></span><small>{{ item.busy ? '正在检查…' : item.status }}<template v-if="!item.complete"> · 统计或监测不完整</template></small></button>
          <button @click="remove(item.id)" :disabled="working" :aria-label="'停止监测 ' + item.path">停止监测</button>
        </article>
      </div>
      <p v-if="!roots.length" class="empty-state">添加目录后自动建立基线，再按目录变化检查容量。</p>
      <template v-if="root">
        <details v-if="root.errors?.length" class="quiet-details"><summary>检查提示</summary><p v-for="(error,index) in root.errors" :key="index">{{ error }}</p></details>
        <div class="list-header directory-row"><span>选择</span><span>文件 / 位置</span><span>体积</span><span>风险</span></div>
        <VirtualList ref="fileList" :load="loadRows" :revision="revision" row-key="id" @error="emit('error',$event)" empty="暂无文件；等待基线检查或目录变化">
          <template #default="{row}"><div class="data-row directory-row"><input type="checkbox" :checked="chosen.has(row.path)" :disabled="row.readonly || working" :aria-label="'选择 ' + row.name" @change="toggle(chosen,row.path)"><span class="truncate" :title="row.path + '\n' + row.reason"><b>{{ row.name }}</b><small>{{ row.path }}</small></span><span>{{ size(row.bytes) }}</span><span :class="['risk',row.risk]">{{ riskName(row.risk) }}{{ row.readonly ? ' · 只读' : '' }}</span></div></template>
        </VirtualList>
        <div class="selection-bar"><button @click="chosen = new Set()" :disabled="working || !chosen.size">清空选择（{{ chosen.size }}）</button><button class="primary" @click="makePreview" :disabled="working || !chosen.size">预览回收</button></div>
      </template>
    </template>
    <template v-else>
      <div class="toolbar"><label><input v-model="showHistory" type="checkbox">显示历史记录</label><span class="muted">恢复不会覆盖已存在的文件；需要时可填写其他完整路径。</span></div>
      <VirtualList ref="vaultList" :load="loadVault" :revision="vaultRevision" row-key="id" :row-height="88" empty="暂无应用回收记录">
        <template #default="{row}"><div class="data-row vault-row"><input type="checkbox" :checked="vaultChosen.has(row.id)" :disabled="working || row.status !== 'vaulted'" :aria-label="'选择 ' + row.original" @change="toggle(vaultChosen,row.id)"><span class="truncate" :title="row.original + '\n' + row.vault_path"><b>{{ row.original }}</b><small>{{ status(row.status) }} · {{ row.source === 'portable' ? '便携应用' : '目录配额' }} · {{ new Date(row.timestamp * 1000).toLocaleString() }}</small><small v-if="row.error">{{ row.error }}</small></span><span>{{ size(row.bytes) }}</span><button @click="askRestore(row)" :disabled="working || row.status !== 'vaulted'">恢复</button></div></template>
      </VirtualList>
      <div class="selection-bar"><span class="muted">永久删除后可能释放空间；实际可用空间由文件系统决定。</span><button class="danger" @click="askPurge" :disabled="working || !vaultChosen.size">预览永久删除（{{ vaultChosen.size }}）</button></div>
    </template>
    <details v-if="state.errors?.length" class="quiet-details"><summary>服务提示</summary><p v-for="(error,index) in state.errors" :key="index">{{ error }}</p></details>
    <Teleport to="body">
      <div v-if="preview || permanent || restoring" class="modal-backdrop"><section class="modal" role="dialog" aria-modal="true" :aria-label="permanent ? '永久删除确认' : restoring ? '恢复确认' : '应用回收预览'">
        <div class="section-heading"><h2>{{ permanent ? '永久删除所选项目' : restoring ? '恢复回收项目' : '移入应用回收站' }}</h2><button @click="preview = null; permanent = null; restoring = null" :disabled="working" aria-label="关闭">✕</button></div>
        <template v-if="preview"><p>{{ preview.message }}</p><p>{{ preview.count }} 项 · {{ size(preview.bytes) }}</p><div class="preview-files"><div v-for="item in preview.files" :key="item.path" class="preview-file"><span class="truncate" :title="item.path">{{ item.path }}</span><span>{{ size(item.bytes) }}</span></div></div><details v-if="preview.notes?.length" class="quiet-details"><summary>风险依据</summary><p v-for="(note,index) in preview.notes" :key="index">{{ riskName(note.risk) }} · {{ note.name }} · {{ note.reason }}</p></details></template>
        <template v-if="permanent"><p>以下项目将永久删除，无法通过应用回收站恢复。</p><p>{{ permanent.length }} 项 · {{ size(permanent.reduce((sum,item) => sum + item.bytes,0)) }}</p><div class="preview-files"><div v-for="item in permanent" :key="item.id" class="preview-file"><span class="truncate" :title="item.original">{{ item.original }}</span><span>{{ size(item.bytes) }}</span></div></div></template>
        <template v-if="restoring"><p class="truncate" :title="restoring.original">原位置：{{ restoring.original }}</p><label class="directory-destination">恢复到完整路径<input v-model="destination" aria-label="恢复目标完整路径"></label><p class="muted">目标已存在时会拒绝恢复，不会覆盖。仅支持同卷恢复。</p></template>
        <label class="medium-confirm"><input v-model="confirmed" type="checkbox">{{ permanent ? '我已核对清单，确认不可恢复地永久删除。' : restoring ? '我已核对恢复位置。' : '我已核对具体清单，确认移入应用回收站。' }}</label>
        <div class="modal-actions"><button @click="preview = null; permanent = null; restoring = null" :disabled="working">取消</button><button :class="permanent ? 'danger' : 'primary'" :disabled="working || !confirmed || (restoring && !destination.trim()) || (permanent && !permanent.length)" @click="permanent ? purge() : restoring ? restore() : recycle()">{{ working ? '正在处理…' : permanent ? '永久删除' : restoring ? '确认恢复' : '确认回收' }}</button></div>
      </section></div>
    </Teleport>
  </section>
</template>

<style scoped>
.directory-governance { min-width: 0; padding: 20px; }
.directory-governance .toolbar { flex-wrap: wrap; }
.directory-path { flex: 1; min-width: 120px; }
.directory-governance input[type=number] { width: 92px; }
.directory-roots { display: grid; gap: 8px; margin: 16px 0; }
.directory-root { display: flex; gap: 12px; align-items: center; border: 1px solid var(--line); border-radius: 8px; padding: 8px; min-width: 0; }
.directory-root.selected { border-color: var(--accent); }
.directory-root-select { display: grid; flex: 1; min-width: 0; gap: 5px; text-align: left; background: transparent; border: 0; }
.directory-root-select small { white-space: normal; }
.directory-row { display: grid; grid-template-columns: 36px minmax(100px,1fr) 100px 110px; gap: 12px; align-items: center; }
.vault-row { display: grid; grid-template-columns: 36px minmax(100px,1fr) 100px 70px; gap: 12px; align-items: center; min-height: 88px; }
.directory-destination { display: grid; gap: 8px; }
.directory-destination input { width: 100%; }
@media(max-width: 720px) { .directory-row,.vault-row { grid-template-columns: 28px minmax(80px,1fr) 74px 76px; gap: 5px; } .directory-governance { padding: 12px; } }
</style>

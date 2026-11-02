<script setup>
import { computed, ref, watch, onBeforeUnmount, onDeactivated } from 'vue'
import { call, size, number, metricSize, riskName } from './api'
import VirtualList from './VirtualList.vue'
import ClickSelect from './ClickSelect.vue'
import AiPanel from './AiPanel.vue'
import DirectoryGovernance from './DirectoryGovernance.vue'
const props = defineProps({ files: { type: Object, default: () => ({}) }, boot: Object, ai: Object, directories: Object, tool: String, title: String })
const emit = defineEmits(['error', 'settings', 'refresh'])
const tool = computed(() => props.tool), scope = ref(''), query = ref(''), risk = ref(''), category = ref(''), largeMinimum = ref(50 * 1024 ** 2), minimum = ref(1048576), currentGroup = ref(null), groups = ref([]), groupVersion = ref(0), rows = ref(), fileTotal = ref(0)
const scanRoots = ref([]), diskMenu = ref(false), working = ref(false), preview = ref(null), mediumConfirmed = ref(false), reports = ref(null), aiFile = ref(null), matrix = ref(null), matrixBusy = ref(false), groupLoading = ref(false)
const toolTitle = computed(() => props.title)
const fileCall = (action, data = {}) => call(action, { ...data, workspace: tool.value })
const selectedIds = ref(new Set()), selectedGroups = ref(new Set()), excludedIds = ref(new Set()), recommended = ref(false)
const aiSnapshot = ref({})
watch(() => props.ai, state => { if (aiFile.value && aiSnapshot.value.target && state?.mode === 'cleanup' && state.request === aiSnapshot.value.request) aiSnapshot.value = state })
let groupRequest = 0, aiRequest = 0
onBeforeUnmount(() => { groupRequest++; if (aiFile.value) closeAi() })
onDeactivated(() => { diskMenu.value = false; if (aiFile.value) closeAi() })
const rowRevision = computed(() => [tool.value, scope.value, query.value, currentGroup.value?.id || '', minimum.value, largeMinimum.value, props.files.generation, groupVersion.value].join('|'))
const scopeOptions = computed(() => [{ value: '', label: '全部扫描位置' }, ...(props.files.roots || []).map(root => ({ value: root, label: root }))])
const metrics = computed(() => props.files.metrics || {})
const scanStatus = computed(() => {
  if (tool.value !== 'duplicates') return props.files.status || props.files.progress?.stage
  if (props.files.busy) return '正在筛选候选文件…'
  if (props.files.status === '已停止') return '已停止比对'
  return props.files.duplicates?.stage || props.files.status
})
const cards = computed(() => [{ label: '扫描文件', number: number(metrics.value.files), unit: '个' }, { label: '扫描体积', ...metricSize(metrics.value.bytes) }, { label: '扫描位置', number: number(metrics.value.targets), unit: '个' }, { label: '可用空间', ...metricSize(metrics.value.free) }])
const hasSelection = computed(() => selectedIds.value.size || selectedGroups.value.size || recommended.value)
const blocked = computed(() => working.value || props.files.busy || props.files.duplicates?.busy || props.files.operation?.busy)
const groupsLoad = async ({ offset, limit }) => ({ total: groups.value.length, rows: groups.value.slice(offset, offset + limit) })
const fileLoad = page => fileCall('file_rows', { ...page, tool: tool.value, group: currentGroup.value?.id || '', scope: scope.value, query: query.value, minimum: tool.value === 'duplicates' ? minimum.value : tool.value === 'large' ? largeMinimum.value : 0 })
function checked(row) { return !row.readonly && row.risk !== 'high' && !excludedIds.value.has(row.id) && (selectedIds.value.has(row.id) || selectedGroups.value.has(row.group) || (recommended.value && !!groups.value.find(group => group.id === row.group)?.recommended)) }
function toggleFile(row) {
  if (checked(row)) { selectedIds.value.delete(row.id); excludedIds.value.add(row.id) }
  else { excludedIds.value.delete(row.id); selectedIds.value.add(row.id) }
}
function toggleGroup(group) { selectedGroups.value.has(group.id) ? selectedGroups.value.delete(group.id) : selectedGroups.value.add(group.id) }
function clearSelection() { selectedIds.value = new Set(); selectedGroups.value = new Set(); excludedIds.value = new Set(); recommended.value = false }
function selection() { return { groups: [...selectedGroups.value], ids: [...selectedIds.value], excluded: [...excludedIds.value], recommended: recommended.value, scope: scope.value, query: query.value, risk: tool.value === 'cleanup' ? risk.value : '', category: tool.value === 'cleanup' ? category.value : '', ...(tool.value === 'duplicates' ? { minimum: Number(minimum.value) } : tool.value === 'large' ? { minimum: Number(largeMinimum.value) } : {}) } }
async function refreshGroups() {
  const request = ++groupRequest
  if (tool.value === 'quota' || tool.value === 'vault') { groupLoading.value = false; return }
  if (tool.value === 'matrix') return refreshMatrix('')
  if (tool.value === 'large') { rows.value?.reload(false); return }
  if (tool.value === 'duplicates' && (props.files.duplicates?.busy || props.files.duplicates?.minimum !== Number(minimum.value))) { groups.value = []; groupVersion.value++; groupLoading.value = false; return }
  groupLoading.value = true
  try { const data = await fileCall('file_groups', { tool: tool.value, scope: scope.value, risk: tool.value === 'cleanup' ? risk.value : '', query: query.value, category: tool.value === 'cleanup' ? category.value : '' }); if (request === groupRequest) { groups.value = Array.isArray(data) ? data : data.groups || []; groupVersion.value++ } }
  catch (error) { emit('error', error.message) }
  finally { if (request === groupRequest) groupLoading.value = false }
}
async function addFolder() { try { const result = await call('pick_folder'); const path = typeof result === 'string' ? result : result?.path; if (path && !scanRoots.value.includes(path)) scanRoots.value.push(path) } catch (error) { emit('error', error.message) } }
async function scan() { working.value = true; try { await fileCall('scan', { roots: [...scanRoots.value], minimum: Number(minimum.value), kind: props.boot.can_disk_scan && scanRoots.value.some(root => props.boot.disks.includes(root)) ? 'disk' : 'folder' }); emit('refresh') } catch (error) { emit('error', error.message) } finally { working.value = false } }
async function cancel() { try { await fileCall('scan_cancel'); emit('refresh') } catch (error) { emit('error', error.message) } }
function selectCopies() { clearSelection(); selectedGroups.value = new Set(groups.value.filter(group => !group.readonly && group.risk !== 'high').map(group => group.id)) }
async function makePreview() { working.value = true; try { preview.value = await fileCall('cleanup_preview', { ids: selection(), tool: tool.value }); mediumConfirmed.value = false } catch (error) { emit('error', error.message) } finally { working.value = false } }
async function execute() { working.value = true; try { const report = await fileCall('cleanup_execute', { token: preview.value.token, confirm_medium: mediumConfirmed.value }); preview.value = null; clearSelection(); reports.value = [report]; emit('refresh'); await refreshGroups() } catch (error) { emit('error', error.message) } finally { working.value = false } }
async function showReports() { try { const result = await fileCall('reports'); reports.value = Array.isArray(result) ? result : result.reports || [] } catch (error) { emit('error', error.message) } }
async function closeAi() { aiRequest++; aiFile.value = null; try { await call('ai_cancel'); emit('refresh') } catch (error) { emit('error', error.message) } }
async function askAi(row, group = false) {
  const request = ++aiRequest
  aiSnapshot.value = {}
  aiFile.value = { workspace: tool.value, tool: tool.value, scope: scope.value, ...(group ? { group: row.id } : { id: row.id }) }
  if (!props.boot.has_key) return
  try { const result = await call('ai', { mode: 'cleanup', file: aiFile.value }); if (request === aiRequest) aiSnapshot.value = result; emit('refresh') } catch (error) { emit('error', error.message) }
}
async function refreshMatrix(relative = '') { if (!props.files.roots?.length || props.files.busy) { matrix.value = null; return } matrixBusy.value = true; try { matrix.value = await fileCall('matrix', { scope: scope.value, relative }) } catch (error) { emit('error', error.message) } finally { matrixBusy.value = false } }
const tiles = computed(() => {
  const nodes = (matrix.value?.nodes || []).filter(node => node.bytes > 0), result = []
  function split(items, x, y, w, h) {
    if (!items.length) return
    if (items.length === 1) { result.push({ ...items[0], x, y, w, h }); return }
    const sum = items.reduce((total, node) => total + node.bytes, 0)
    let half = 0, at = 0
    while (at < items.length - 1 && half < sum / 2) half += items[at++].bytes
    const ratio = half / sum
    if (w >= h) { split(items.slice(0, at), x, y, w * ratio, h); split(items.slice(at), x + w * ratio, y, w * (1 - ratio), h) }
    else { split(items.slice(0, at), x, y, w, h * ratio); split(items.slice(at), x, y + h * ratio, w, h * (1 - ratio)) }
  }
  split(nodes, 0, 0, 100, 100); return result
})
function date(value) { if (value == null) return '—'; return new Date(Number(value) / 1e6).toLocaleDateString('zh-CN') }
watch([tool, scope, risk, category, query], () => { currentGroup.value = null; clearSelection(); refreshGroups() })
watch(largeMinimum, () => { clearSelection(); currentGroup.value = null })
watch(minimum, () => { groupRequest++; clearSelection(); currentGroup.value = null; if (tool.value === 'duplicates') { groups.value = []; groupVersion.value++ } })
watch(() => props.files.generation, () => { clearSelection(); currentGroup.value = null; preview.value = null; refreshGroups() })
watch(() => props.files.busy, (busy, before) => { if (before && !busy) refreshGroups() })
watch(() => [props.files.duplicates?.job, props.files.duplicates?.busy, props.files.duplicates?.complete, props.files.duplicates?.minimum, props.files.duplicates?.scope].join('|'), () => { if (tool.value === 'duplicates') { currentGroup.value = null; clearSelection(); refreshGroups() } })
refreshGroups()
</script>
<template>
    <div class="file-workspace">
    <DirectoryGovernance v-if="tool === 'quota' || tool === 'vault'" :mode="tool" :state="directories || {}" :boot="boot" @error="emit('error',$event)" @refresh="emit('refresh')"/>
    <template v-else>
    <section class="panel scan-controls" aria-label="扫描位置"><div class="section-heading"><h2>{{ toolTitle }}</h2><button @click="showReports">回收报告</button></div>
    <div class="toolbar scan-toolbar"><button @click="addFolder" :disabled="blocked">＋ 添加文件夹</button><div v-if="boot.can_disk_scan" class="select"><button @click="diskMenu = !diskMenu" :disabled="blocked">选择磁盘 ⌄</button><div v-if="diskMenu" class="select-menu disk-menu"><label v-for="disk in boot.disks" :key="disk"><input type="checkbox" :value="disk" v-model="scanRoots">{{ disk }}</label><button @click="diskMenu = false">完成</button></div></div><span class="root-caption truncate" :title="scanRoots.join('\n')">{{ scanRoots.length ? scanRoots.join('、') : '选择一个或多个扫描位置' }}</span><button v-if="scanRoots.length" class="icon-button" @click="scanRoots = []" :disabled="blocked" title="清空扫描位置">✕</button><ClickSelect v-if="tool === 'duplicates'" v-model="minimum" :disabled="blocked" :options="[{value:512000,label:'≥ 500 KB'},{value:1048576,label:'≥ 1 MB'},{value:52428800,label:'≥ 50 MB'},{value:209715200,label:'≥ 200 MB'}]" label="重复文件最小体积"/><button class="primary" @click="scan" :disabled="blocked || !scanRoots.length">{{ tool === 'duplicates' ? '开始比对' : '开始扫描' }}</button><button v-if="files.busy || files.duplicates?.busy" @click="cancel">{{ tool === 'duplicates' ? '停止比对' : '停止扫描' }}</button></div></section>
    <div class="metrics"><div v-for="card in cards" :key="card.label" class="metric panel"><span>{{ card.label }}</span><div><strong>{{ card.number }}</strong><small>{{ card.unit }}</small></div></div></div>
    <div v-if="files.busy || files.status" class="scan-status"><span>{{ scanStatus }}</span><span v-if="files.busy" class="muted">{{ number(files.progress?.done) }}<template v-if="files.progress?.total"> / {{ number(files.progress.total) }}</template></span><progress v-if="files.busy" :value="files.progress?.total ? files.progress.done : undefined" :max="files.progress?.total || 1"/></div>
    <section class="panel files-content">
      <div class="toolbar filters"><ClickSelect v-model="scope" :options="scopeOptions" label="扫描范围"/><template v-if="tool !== 'matrix'"><input v-model="query" aria-label="搜索文件" placeholder="搜索文件名称或路径"><ClickSelect v-if="tool === 'cleanup'" v-model="category" :options="[{value:'',label:'全部类别'},...(files.categories || []).map(item => ({value:item.id,label:item.name}))]" label="类别筛选"/><ClickSelect v-if="tool === 'cleanup'" v-model="risk" :options="[{value:'',label:'全部风险'},{value:'low',label:'低风险'},{value:'medium',label:'中风险'},{value:'high',label:'高风险 · 只读'}]" label="风险筛选"/></template><ClickSelect v-if="tool === 'large'" v-model="largeMinimum" :options="[1,10,50,100,500].map(value => ({value:value * 1024 ** 2,label:'≥ ' + value + ' MiB'}))" label="大文件最小体积"/></div>
      <div v-if="tool === 'duplicates' && files.duplicates?.busy" class="inline-note">{{ files.duplicates.stage }} · {{ number(files.duplicates.done) }} / {{ number(files.duplicates.total) }}</div>
      <template v-if="tool === 'matrix'"><div class="toolbar"><button :disabled="!matrix?.relative || matrixBusy" @click="refreshMatrix(matrix?.parent || '')">↑ 上一级</button><span class="truncate muted" :title="matrix?.path">{{ matrix?.path || '扫描后查看空间分布' }}</span></div><div class="matrix"><div v-for="(node,index) in tiles" :key="node.id || index" class="matrix-tile" :class="{readonly:node.readonly}" :style="{left:node.x+'%',top:node.y+'%',width:node.w+'%',height:node.h+'%',background:`var(--map-${index % 5})`}" :title="node.name + ' · ' + size(node.bytes)"><button class="matrix-open" :disabled="node.readonly || !node.directory || matrixBusy" @click="refreshMatrix(node.relative)"><b>{{ node.name }}</b><span>{{ size(node.bytes) }}</span></button></div><div v-if="!tiles.length" class="empty-state">{{ matrixBusy ? '正在生成矩阵…' : '暂无空间分布' }}</div></div></template>
      <template v-else-if="!currentGroup && tool !== 'large'"><div class="list-header group-grid"><span>选择</span><span>分类</span><span>风险</span><span>文件数</span><span>体积</span><span>AI</span></div><VirtualList :load="groupsLoad" :revision="groupVersion" :empty="groupLoading ? '正在整理分组…' : tool === 'duplicates' ? '选择位置后开始比对' : '扫描后显示分类结果'"><template #default="{row}"><div class="data-row group-grid"><input type="checkbox" :aria-label="'选择' + row.name" :disabled="row.readonly || row.risk === 'high' || blocked" :checked="selectedGroups.has(row.id) || (recommended && row.recommended)" @change="toggleGroup(row)"><button class="group-open truncate" :title="row.reason" @click="currentGroup = row"><b>{{ row.name }}</b><small>{{ row.keep_one ? '保留至少一份' : row.reason }}</small></button><span :class="['risk',row.risk]">{{ riskName(row.risk) }}</span><span>{{ number(row.count) }}</span><span>{{ size(row.bytes) }}</span><button class="ai-icon" :aria-label="'AI 分析' + row.name" @click="askAi(row,true)">✦</button></div></template></VirtualList></template>
      <template v-else><div v-if="currentGroup" class="toolbar breadcrumb"><button @click="currentGroup = null">← 返回分组</button><b class="truncate">{{ currentGroup.name }}</b><span class="muted">{{ number(fileTotal) }} 个文件</span></div><div class="list-header file-grid"><span>选择</span><span>文件 / 位置</span><span>体积</span><span>修改日期</span><span>风险</span><span>AI</span></div><VirtualList ref="rows" :load="fileLoad" :revision="rowRevision" @total="fileTotal = $event" @error="emit('error',$event)" empty="没有匹配的文件"><template #default="{row}"><div class="data-row file-grid"><input type="checkbox" :aria-label="'选择' + row.name" :disabled="row.readonly || row.risk === 'high' || blocked" :checked="checked(row)" @change="toggleFile(row)"><span class="truncate" :title="row.path + '\n' + (row.reason || '')"><b>{{ row.name }}</b><small>{{ row.path }}</small></span><span>{{ size(row.bytes) }}</span><span>{{ date(row.modified) }}</span><span :class="['risk',row.risk]">{{ riskName(row.risk) }}</span><button class="ai-icon" :aria-label="'AI 分析' + row.name" @click="askAi(row)">✦</button></div></template></VirtualList></template>
      <div v-if="tool !== 'matrix'" class="selection-bar"><div class="row gap"><button v-if="tool === 'cleanup'" :class="{active:recommended}" @click="recommended = !recommended" :disabled="blocked">{{ recommended ? '✓ 已选推荐项' : '选择推荐项' }}</button><button v-if="tool === 'duplicates'" @click="selectCopies" :disabled="blocked || files.duplicates?.busy || !groups.length">选择副本（每组留一份）</button><button @click="clearSelection" :disabled="!hasSelection || blocked">清空选择</button><span v-if="hasSelection" class="muted">{{ selectedGroups.size }} 组 · {{ selectedIds.size }} 项<template v-if="recommended"> · 推荐项</template></span></div><button class="primary" :disabled="!hasSelection || blocked" @click="makePreview">{{ working ? '正在核验…' : '预览回收' }}</button></div>
    </section>
    <details v-if="files.errors?.length" class="quiet-details"><summary>扫描提示（{{ files.errors.length }}）</summary><p v-for="(error,index) in files.errors.slice(0,20)" :key="index">{{ error }}</p></details>
    </template>
    <Teleport to="body">
      <div v-if="preview" class="modal-backdrop"><section class="modal" role="dialog" aria-modal="true" aria-label="回收预览"><div class="section-heading"><h2>确认回收</h2><button :disabled="working" @click="preview = null" aria-label="关闭">✕</button></div><div class="preview-total"><strong>{{ number(preview.count) }}</strong><span>个文件</span><strong>{{ size(preview.bytes) }}</strong></div><p class="muted">移入系统回收站。同盘回收不保证立即释放磁盘空间。</p><div class="preview-files"><div v-for="(file,index) in preview.files || []" :key="index" class="preview-file"><span class="truncate" :title="file.path">{{ file.path }}</span><span>{{ size(file.bytes) }}</span></div></div><details v-if="preview.notes?.length" class="quiet-details"><summary>分类依据与风险</summary><p v-for="(note,index) in preview.notes" :key="index">{{ note.name }} · {{ riskName(note.risk) }} · {{ note.reason }}</p></details><label v-if="preview.medium" class="medium-confirm"><input type="checkbox" v-model="mediumConfirmed">我已核对中风险文件，单独确认回收这些项目。</label><div class="modal-actions"><button :disabled="working" @click="preview = null">取消</button><button class="danger" :disabled="working || !preview.count || (!!preview.medium && !mediumConfirmed)" @click="execute">{{ working ? '正在回收…' : '确认移入回收站' }}</button></div></section></div>
      <div v-if="reports !== null" class="modal-backdrop" @click.self="reports = null"><section class="modal" role="dialog" aria-modal="true" aria-label="回收报告"><div class="section-heading"><h2>回收报告</h2><button @click="reports = null" aria-label="关闭">✕</button></div><div class="detail-scroll"><p v-if="!reports.length" class="muted">暂无回收记录</p><article v-for="(report,index) in reports" :key="index" class="report"><h3>{{ report.message || '回收结果' }}</h3><p>成功 {{ number(report.success) }} · 跳过 {{ number(report.skipped) }} · 失败 {{ number(report.failed) }} · {{ size(report.bytes) }}</p><p class="muted">可用空间：{{ size(report.before_free) }} → {{ size(report.after_free) }}</p><details class="quiet-details"><summary>逐项结果</summary><p v-for="(result,i) in report.results || []" :key="i">{{ result.path }} · {{ result.state || result.reason || result.message }}</p></details></article></div></section></div>
      <div v-if="aiFile" class="modal-backdrop" @click.self="closeAi"><section class="modal ai-modal" role="dialog" aria-modal="true" aria-label="文件 AI 分析"><div class="section-heading"><h2>文件分析</h2><button @click="closeAi" aria-label="关闭">✕</button></div><AiPanel :state="aiSnapshot" :file="aiFile" :has-key="boot.has_key" selected automatic @settings="aiFile = null; emit('settings')" @error="emit('error',$event)"/></section></div>
    </Teleport>
  </div>
</template>

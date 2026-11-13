<script setup>
import { computed, ref, onBeforeUnmount } from 'vue'
import { call, size, number, metricSize, percentage } from './api'
import VirtualList from './VirtualList.vue'
import ClickSelect from './ClickSelect.vue'
import TrendChart from './TrendChart.vue'
import AiPanel from './AiPanel.vue'
import ProcessGovernance from './ProcessGovernance.vue'
import BlockTrace from './BlockTrace.vue'
const props = defineProps({ memory: { type: Object, default: () => ({}) }, ai: Object, boot: Object, control: Object })
const emit = defineEmits(['error', 'settings', 'refresh'])
const view = ref('list')
const query = ref(''), sort = ref('rss'), processes = ref(), detail = ref(''), detailTab = ref('metrics'), trend = ref('rss'), stopping = ref(null), working = ref(false)
const selected = computed(() => props.memory.selected || {})
const info = computed(() => selected.value.details || {})
const report = computed(() => selected.value.report || {})
const metrics = computed(() => report.value.metrics || {})
const trace = computed(() => selected.value.trace || {})
const traceFiles = computed(() => [...new Set([
  ...(report.value.mappings || []), ...(report.value.open_files || []),
].map(row => row.path).filter(path => /\.(gguf|db|sqlite3?)(?:-wal)?$|\/blobs\/sha256-/i.test(path || '')))])
const icons = ref({})
let iconTimer, iconRequest = 0, closed = false
function visibleIcons(rows) {
  clearTimeout(iconTimer)
  const request = ++iconRequest
  const missing = rows.filter(row => row.key?.[0]>0 && row.key?.[1]>0 && !Object.hasOwn(icons.value, row.id)).slice(0,32)
  if (!missing.length || closed) return
  const load = async () => {
    if (closed || request !== iconRequest) return
    try {
      const result = await call('process_icons', { keys: missing.map(row => row.key) })
      if (closed || request !== iconRequest) return
      icons.value = { ...icons.value, ...result.icons }
      if (Object.keys(icons.value).length > 512) icons.value = Object.fromEntries(rows.filter(row => Object.hasOwn(icons.value,row.id)).map(row => [row.id, icons.value[row.id]]))
      if (result.pending) iconTimer = setTimeout(load, 250)
    } catch { /* A sampled process can exit before its icon is requested. */ }
  }
  iconTimer = setTimeout(load, 40)
}
onBeforeUnmount(() => { closed = true; iconRequest++; clearTimeout(iconTimer) })
const summaryItems = computed(() => [
  { label: '物理内存', ...metricSize(props.memory.total) },
  { label: '可用内存', ...metricSize(props.memory.available) },
  { label: '内存使用率', number: number(props.memory.percent), unit: '%' },
  { label: '运行进程', number: number(props.memory.process_count), unit: '个' },
])
const metricLabels = { rss: '驻留内存 RSS', uss: '私有驻留 USS', pss: '比例分摊 PSS', anonymous: '匿名内存', file_pss: '文件映射 PSS', shared_pss: '共享内存 PSS', swap: '交换占用', private_commit: '私有提交', cpu_percent: 'CPU · 整机', cpu_user_percent: '用户态 · 整机', cpu_system_percent: '内核态 · 整机', read_bps: '读取字节速率', write_bps: '写入字节速率', read_iops: '读取操作速率', write_iops: '写入操作速率', read_chars_bps: '逻辑读取速率', write_chars_bps: '逻辑写入速率', threads: '线程数', resources: 'FD / 句柄', ctx_voluntary_rate: '主动上下文切换', ctx_involuntary_rate: '被动上下文切换' }
const overview = computed(() => [
  { label: '内存占用', value: size(metrics.value.rss ?? info.value.rss ?? selected.value.rss) },
  { label: 'CPU', value: percentage(metrics.value.cpu_percent) },
  { label: props.boot.platform === 'Linux' ? '磁盘读取' : 'I/O 读取', value: formatMetric('read_bps', metrics.value.read_bps) },
  { label: props.boot.platform === 'Linux' ? '磁盘写入' : 'I/O 写入', value: formatMetric('write_bps', metrics.value.write_bps) },
])
const memoryStats = computed(() => [
  { label: '内存峰值', value: size(info.value.memory?.peak) },
  { label: '内存变化', value: size(info.value.memory?.delta) },
])
const extraMetrics = computed(() => Object.entries(metricLabels)
  .filter(([key]) => !['rss', 'cpu_percent', 'read_bps', 'write_bps'].includes(key) && metrics.value[key] != null)
  .map(([key, label]) => ({ key, label, value: formatMetric(key, metrics.value[key]) })))
function formatMetric(key, value) { if (value == null) return '—'; return key.includes('percent') ? percentage(value) : key.endsWith('_bps') ? `${size(value)}/秒` : ['threads', 'resources'].includes(key) || key.endsWith('iops') || key.endsWith('_rate') ? number(value) : size(value) }
const load = page => call('process_rows', { ...page, query: query.value, sort: sort.value, tree: view.value === 'tree' })
const localLoad = computed(() => {
  const rows = detailTab.value === 'threads' ? report.value.threads || [] : detailTab.value === 'files' ? report.value.open_files || [] : detailTab.value === 'trace' ? trace.value.events || [] : report.value.mappings || []
  return async ({ offset, limit }) => ({ total: rows.length, rows: rows.slice(offset, offset + limit) })
})
async function select(row) { try { await call('process_select', { key: row.key }); emit('refresh') } catch (error) { emit('error', error.message) } }
async function stop() {
  working.value = true
  try { await call('process_stop', { key: stopping.value.key, force: stopping.value.force, confirmed: true }); stopping.value = null; emit('refresh'); processes.value?.reload(false) }
  catch (error) { emit('error', error.message) }
  finally { working.value = false }
}
async function toggleTrace() { try { await call('process_trace', { enabled: !trace.value.active }); emit('refresh') } catch (error) { emit('error', error.message) } }
function openDetail(tab = 'metrics') { detailTab.value = tab; detail.value = 'resource' }
function sameKey(a, b) { return a && b && a[0] === b[0] && a[1] === b[1] }
defineExpose({ refresh: () => processes.value?.reload(false) })
</script>
<template>
  <div class="page memory-page">
    <div class="metrics"><div v-for="card in summaryItems" :key="card.label" class="metric panel"><span>{{ card.label }}</span><div><strong>{{ card.number }}</strong><small>{{ card.unit }}</small></div></div></div>
    <div class="memory-columns">
      <section class="panel process-panel">
        <div class="section-heading"><h2>进程</h2><span class="muted">{{ number(memory.process_count) }} 个</span></div>
        <div class="toolbar"><ClickSelect v-model="view" label="进程视图" :options="[{value:'list',label:'进程列表'},{value:'tree',label:'进程树'}]"/><input v-model="query" aria-label="搜索进程" placeholder="搜索进程名称或 PID"><ClickSelect v-model="sort" label="进程排序" :options="[{value:'rss',label:'内存占用'},{value:'delta',label:'内存变化'},{value:'name',label:'名称'},{value:'pid',label:'PID'}]"/></div>
        <div class="list-header process-grid"><span>进程 / PID</span><span>内存</span><span>变化</span></div>
        <VirtualList ref="processes" row-key="id" :load="load" :revision="query + sort + view" @visible="visibleIcons" empty="没有匹配的进程">
          <template #default="{row}"><button class="data-row process-grid" :class="{selected:sameKey(row.key, selected.key)}" @click="select(row)"><span class="process-name" :style="view === 'tree' ? {paddingLeft: Math.min(row.depth || 0, 8) * 12 + 'px'} : undefined"><img v-if="icons[row.id]" class="process-icon" :src="icons[row.id]" alt=""><svg v-else class="process-icon fallback-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/></svg><span class="truncate"><b>{{ row.name }}</b><small>PID {{ row.key?.[0] }} · {{ number(row.seconds) }} 秒</small></span></span><span>{{ size(row.rss) }}</span><span :class="{'positive':row.delta > 0}">{{ row.delta > 0 ? '+' : '' }}{{ size(row.delta) }}</span></button></template>
        </VirtualList>
      </section>
      <div class="process-right">
        <section class="panel selected-summary">
          <div class="section-heading"><div class="truncate"><h2>{{ selected.name || '选择一个进程' }}</h2><span v-if="selected.key" class="muted">PID {{ selected.key[0] }}</span></div><button v-if="selected.key" @click="openDetail()">资源明细 ↗</button></div>
          <template v-if="selected.key">
            <div class="selected-metrics"><div v-for="item in overview" :key="item.label"><span>{{ item.label }}</span><b>{{ item.value }}</b></div></div>
            <div class="row gap wrap">
              <button v-if="boot.platform === 'Linux'" @click="openDetail('block')">块 I/O 追踪<span v-if="selected.block_trace?.active"> · 采集中</span></button>
              <button class="danger-quiet" @click="stopping = {key:[...selected.key],name:selected.name,force:false}">结束进程</button>
              <button v-if="boot.can_force_stop" class="danger-quiet" @click="stopping = {key:[...selected.key],name:selected.name,force:true}">强制结束…</button>
            </div>
          </template>
          <p v-if="selected.message" class="inline-note" role="status">{{ selected.message }}</p>
        </section>
        <AiPanel compact :state="ai?.mode === 'cleanup' ? {} : ai" :has-key="boot.has_key" :selected="!!selected.key && !!selected.summary" @settings="emit('settings')" @error="emit('error',$event)"/>
        <ProcessGovernance v-if="selected.key" :memory="memory" :state="control || {}" :boot="boot" @error="emit('error',$event)" @refresh="emit('refresh')"/>
      </div>
    </div>
    <Teleport to="body"><div v-if="detail" class="modal-backdrop" @click.self="detail = ''"><section class="modal wide" role="dialog" aria-modal="true" aria-label="进程资源明细"><div class="section-heading"><h2>{{ selected.name }} · 资源明细</h2><button @click="detail = ''" aria-label="关闭">✕</button></div><div class="tabs compact"><button v-for="[value,label] in [['metrics','概览'],['threads','线程'],['files','打开文件'],['mappings','内存映射'],['trend','趋势'],['findings','依据'],...(boot.can_trace ? [['trace','文件跟踪']] : []),...(boot.platform === 'Linux' ? [['block','块 I/O 追踪']] : [])]" :key="value" :class="{active:detailTab === value}" @click="detailTab = value">{{ label }}</button></div>
      <div v-if="detailTab === 'metrics'" class="detail-scroll">
        <div class="resource-overview">
          <div v-for="item in [...overview, ...memoryStats]" :key="item.label">
            <span class="muted">{{ item.label }}</span>
            <strong>{{ item.value }}</strong>
          </div>
        </div>
        <details class="quiet-details">
          <summary>详细指标</summary>
          <div class="detail-metrics">
            <div v-for="item in extraMetrics" :key="item.key"><span>{{ item.label }}</span><strong>{{ item.value }}</strong></div>
          </div>
        </details>
        <details class="quiet-details">
          <summary>进程信息</summary>
          <div class="detail-metrics">
            <div><span>程序路径</span><strong class="truncate" :title="info.path">{{ info.path || '—' }}</strong></div>
            <div><span>启动时间</span><strong>{{ info.started_at ? new Date(info.started_at * 1000).toLocaleString('zh-CN') : '—' }}</strong></div>
            <div><span>父进程</span><strong>{{ info.parent_name || '—' }} · {{ info.parent_pid ?? '—' }}</strong></div>
            <div><span>状态</span><strong>{{ info.status || '—' }}</strong></div>
          </div>
        </details>
      </div>
      <BlockTrace v-else-if="detailTab === 'block'" :process-key="selected.key" :trace="selected.block_trace" :files="traceFiles" @file-access="detailTab = 'trace'" @refresh="emit('refresh')"/>
      <template v-else-if="detailTab === 'trend'"><ClickSelect v-model="trend" :options="Object.entries(metricLabels).map(([value,label])=>({value,label}))" label="趋势指标"/><TrendChart :points="report.curves?.[trend] || []" :label="metricLabels[trend]"/><p class="muted">{{ number(report.samples) }} 次采样 · {{ number(report.seconds) }} 秒</p></template>
      <div v-else-if="detailTab === 'findings'" class="detail-scroll"><article v-for="(finding,index) in report.findings || []" :key="index" class="finding"><h3>{{ finding.title }}</h3><p>{{ finding.evidence }}</p></article><p v-if="!report.findings?.length" class="muted">继续采样后显示观察依据。</p><p v-for="(error,index) in report.errors || []" :key="index" class="muted">{{ error }}</p></div>
      <template v-else><div v-if="detailTab === 'trace'" class="toolbar"><button @click="toggleTrace">{{ trace.active ? '停止跟踪' : '开始文件跟踪' }}</button><span class="muted">{{ trace.status?.split('；读写系统调用')[0] }}</span><span v-if="trace.dropped" class="muted">丢弃 {{ trace.dropped }} 条</span></div><div class="list-header detail-grid"><span>{{ detailTab === 'threads' ? '线程 ID' : detailTab === 'trace' ? '操作 / TID' : '路径' }}</span><span>{{ detailTab === 'threads' ? '总 CPU' : detailTab === 'mappings' ? 'RSS / PSS' : detailTab === 'files' ? 'FD / 模式' : '字节 / 耗时' }}</span><span>{{ detailTab === 'threads' ? '用户 / 内核' : detailTab === 'mappings' ? '私有驻留' : detailTab === 'files' ? '偏移' : '路径' }}</span></div><VirtualList :key="detailTab" :load="localLoad" :revision="String(report.deep_at) + ':' + (trace.events?.length || 0)" empty="当前没有可读取的记录"><template #default="{row}"><div class="data-row detail-grid"><template v-if="detailTab === 'threads'"><span>{{ row.tid }}</span><span>{{ percentage(row.cpu_percent) }}</span><span>{{ percentage(row.user_percent) }} / {{ percentage(row.system_percent) }}</span></template><template v-else-if="detailTab === 'mappings'"><span class="truncate" :title="row.path">{{ row.path }}</span><span>{{ size(row.rss) }} / {{ size(row.pss) }}</span><span>{{ size(row.private) }}</span></template><template v-else-if="detailTab === 'files'"><span class="truncate" :title="row.path">{{ row.path }}</span><span>{{ row.fd }} / {{ row.mode || '—' }}</span><span>{{ number(row.position) }}</span></template><template v-else><span>{{ row.operation }} / {{ row.tid }}</span><span>{{ size(row.bytes_count) }} / {{ row.duration_seconds == null ? '—' : number(row.duration_seconds * 1000) + 'ms' }}</span><span class="truncate" :title="row.path">{{ row.path }}</span></template></div></template></VirtualList></template>
    </section></div>
    <div v-if="stopping" class="modal-backdrop"><section class="modal small" role="alertdialog" aria-modal="true"><h2>{{ stopping.force ? '强制结束进程？' : '结束进程？' }}</h2><p>{{ stopping.name }} · PID {{ stopping.key[0] }}</p><p>{{ stopping.force ? '强制结束不会等待程序正常退出，未保存的数据可能丢失。' : '程序可能有未保存的数据。请确认已经完成保存。' }}</p><div class="modal-actions"><button :disabled="working" @click="stopping = null">取消</button><button class="danger" :disabled="working" @click="stop">{{ working ? '正在结束…' : stopping.force ? '确认强制结束' : '确认结束' }}</button></div></section></div></Teleport>
  </div>
</template>

<style scoped>
.selected-metrics{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.selected-metrics>div{padding:9px 12px;background:var(--raised);border-radius:8px}
.resource-overview{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-bottom:16px}
.resource-overview>div{display:grid;gap:7px;background:var(--raised);border-radius:8px;padding:13px}
.resource-overview span{font-size:12px}
.resource-overview strong{font-size:19px}
.quiet-details+.quiet-details{margin-top:12px}
</style>

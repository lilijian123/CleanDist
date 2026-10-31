<script setup>
import { computed, ref, onBeforeUnmount } from 'vue'
import { call, size, number, metricSize, percentage } from './api'
import VirtualList from './VirtualList.vue'
import ClickSelect from './ClickSelect.vue'
import TrendChart from './TrendChart.vue'
import AiPanel from './AiPanel.vue'
const props = defineProps({ memory: { type: Object, default: () => ({}) }, ai: Object, boot: Object })
const emit = defineEmits(['error', 'settings', 'refresh'])
const query = ref(''), sort = ref('rss'), processes = ref(), detail = ref(''), detailTab = ref('metrics'), trend = ref('rss'), stopping = ref(null), working = ref(false)
const selected = computed(() => props.memory.selected || {})
const info = computed(() => selected.value.details || {})
const report = computed(() => selected.value.report || {})
const metrics = computed(() => report.value.metrics || {})
const trace = computed(() => selected.value.trace || {})
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
const metricEntries = computed(() => Object.entries(metricLabels).map(([key, label]) => ({ key, label, value: formatMetric(key, metrics.value[key]) })))
function formatMetric(key, value) { if (value == null) return '—'; return key.includes('percent') ? percentage(value) : key.endsWith('_bps') ? `${size(value)}/秒` : ['threads', 'resources'].includes(key) || key.endsWith('iops') || key.endsWith('_rate') ? number(value) : size(value) }
const load = page => call('process_rows', { ...page, query: query.value, sort: sort.value })
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
function sameKey(a, b) { return a && b && a[0] === b[0] && a[1] === b[1] }
defineExpose({ refresh: () => processes.value?.reload(false) })
</script>
<template>
  <div class="page memory-page">
    <div class="metrics"><div v-for="card in summaryItems" :key="card.label" class="metric panel"><span>{{ card.label }}</span><div><strong>{{ card.number }}</strong><small>{{ card.unit }}</small></div></div></div>
    <div class="memory-columns">
      <section class="panel process-panel">
        <div class="section-heading"><h2>进程</h2><span class="muted">{{ number(memory.process_count) }} 个</span></div>
        <div class="toolbar"><input v-model="query" aria-label="搜索进程" placeholder="搜索进程名称或 PID"><ClickSelect v-model="sort" label="进程排序" :options="[{value:'rss',label:'内存占用'},{value:'delta',label:'内存变化'},{value:'name',label:'名称'},{value:'pid',label:'PID'}]"/></div>
        <div class="list-header process-grid"><span>进程 / PID</span><span>内存</span><span>变化</span></div>
        <VirtualList ref="processes" row-key="id" :load="load" :revision="query + sort" @visible="visibleIcons" empty="没有匹配的进程">
          <template #default="{row}"><button class="data-row process-grid" :class="{selected:sameKey(row.key, selected.key)}" @click="select(row)"><span class="process-name"><img v-if="icons[row.id]" class="process-icon" :src="icons[row.id]" alt=""><svg v-else class="process-icon fallback-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/></svg><span class="truncate"><b>{{ row.name }}</b><small>PID {{ row.key?.[0] }} · {{ number(row.seconds) }} 秒</small></span></span><span>{{ size(row.rss) }}</span><span :class="{'positive':row.delta > 0}">{{ row.delta > 0 ? '+' : '' }}{{ size(row.delta) }}</span></button></template>
        </VirtualList>
      </section>
      <div class="process-right">
        <section class="panel selected-summary">
          <div class="section-heading"><div class="truncate"><h2>{{ selected.name || '选择一个进程' }}</h2><span v-if="selected.key" class="muted">PID {{ selected.key[0] }}</span></div><button :disabled="!selected.key" @click="detail = 'resource'">资源明细 ↗</button></div>
          <div v-if="selected.key" class="selected-metrics"><div><span>驻留内存</span><b>{{ size(metrics.rss ?? selected.details?.rss) }}</b></div><div><span>CPU · 整机</span><b>{{ percentage(metrics.cpu_percent) }}</b></div><div><span>观察时间</span><b>{{ number(report.seconds) }} 秒</b></div></div>
          <div v-if="selected.key" class="row gap"><button class="danger-quiet" @click="stopping = {key:[...selected.key],name:selected.name,force:false}">结束进程</button><button v-if="boot.can_force_stop" class="danger-quiet" @click="stopping = {key:[...selected.key],name:selected.name,force:true}">强制结束…</button></div>
          <p v-else class="muted">从左侧选择，开始采样并查看诊断依据。</p><p v-if="selected.message" class="inline-note" role="status">{{ selected.message }}</p>
        </section>
        <AiPanel :state="ai?.mode === 'cleanup' ? {} : ai" :has-key="boot.has_key" :selected="!!selected.key && !!selected.summary" @settings="emit('settings')" @error="emit('error',$event)"/>
      </div>
    </div>
    <Teleport to="body"><div v-if="detail" class="modal-backdrop" @click.self="detail = ''"><section class="modal wide" role="dialog" aria-modal="true" aria-label="进程资源明细"><div class="section-heading"><h2>{{ selected.name }} · 资源明细</h2><button @click="detail = ''" aria-label="关闭">✕</button></div><div class="tabs compact"><button v-for="[value,label] in [['metrics','概览'],['threads','线程'],['files','打开文件'],['mappings','内存映射'],['trend','趋势'],['findings','依据'],...(boot.can_trace ? [['trace','文件跟踪']] : [])]" :key="value" :class="{active:detailTab === value}" @click="detailTab = value">{{ label }}</button></div>
      <div v-if="detailTab === 'metrics'" class="detail-scroll"><div class="detail-metrics"><div v-for="item in metricEntries" :key="item.key"><span>{{ item.label }}</span><strong>{{ item.value }}</strong></div></div><div v-if="selected.details?.memory" class="detail-metrics"><div v-for="[key,label] in [['minimum','观察最低'],['peak','观察峰值'],['mean','平均驻留'],['delta','驻留变化'],['current','当前驻留']]" :key="key"><span>{{ label }}</span><strong>{{ size(selected.details.memory[key]) }}</strong></div></div><details class="quiet-details"><summary>采样口径与进程信息</summary><p>RSS 是驻留内存，不等于私有工作集。CPU 按整机计算；线程 CPU 按单核计算。首次采样与系统未提供的指标显示 —。</p><div class="detail-metrics"><div><span>程序路径</span><strong class="truncate" :title="info.path">{{ info.path || '—' }}</strong></div><div><span>启动时间</span><strong>{{ info.started_at ? new Date(info.started_at * 1000).toLocaleString('zh-CN') : '—' }}</strong></div><div><span>父进程</span><strong>{{ info.parent_name || '—' }} · {{ info.parent_pid ?? '—' }}</strong></div><div><span>状态</span><strong>{{ info.status || '—' }}</strong></div><div><span>内存变化速率</span><strong>{{ info.memory?.rate == null ? '—' : size(info.memory.rate) + '/秒' }}</strong></div></div><p v-for="(error,index) in info.errors || []" :key="index">{{ error }}</p></details></div>
      <template v-else-if="detailTab === 'trend'"><ClickSelect v-model="trend" :options="Object.entries(metricLabels).map(([value,label])=>({value,label}))" label="趋势指标"/><TrendChart :points="report.curves?.[trend] || []" :label="metricLabels[trend]"/><p class="muted">{{ number(report.samples) }} 次采样 · {{ number(report.seconds) }} 秒</p></template>
      <div v-else-if="detailTab === 'findings'" class="detail-scroll"><article v-for="(finding,index) in report.findings || []" :key="index" class="finding"><h3>{{ finding.title }}</h3><p>{{ finding.evidence }}</p></article><p v-if="!report.findings?.length" class="muted">继续采样后显示观察依据。</p><p v-for="(error,index) in report.errors || []" :key="index" class="muted">{{ error }}</p></div>
      <template v-else><div v-if="detailTab === 'trace'" class="toolbar"><button @click="toggleTrace">{{ trace.active ? '停止跟踪' : '开始文件跟踪' }}</button><span class="muted">{{ trace.status }}</span><span v-if="trace.dropped" class="muted">丢弃 {{ trace.dropped }} 条</span></div><div class="list-header detail-grid"><span>{{ detailTab === 'threads' ? '线程 ID' : detailTab === 'trace' ? '操作 / TID' : '路径' }}</span><span>{{ detailTab === 'threads' ? '总 CPU' : detailTab === 'mappings' ? 'RSS / PSS' : detailTab === 'files' ? 'FD / 模式' : '字节 / 耗时' }}</span><span>{{ detailTab === 'threads' ? '用户 / 内核' : detailTab === 'mappings' ? '私有驻留' : detailTab === 'files' ? '偏移' : '路径' }}</span></div><VirtualList :key="detailTab" :load="localLoad" :revision="String(report.deep_at) + ':' + (trace.events?.length || 0)" empty="当前没有可读取的记录"><template #default="{row}"><div class="data-row detail-grid"><template v-if="detailTab === 'threads'"><span>{{ row.tid }}</span><span>{{ percentage(row.cpu_percent) }}</span><span>{{ percentage(row.user_percent) }} / {{ percentage(row.system_percent) }}</span></template><template v-else-if="detailTab === 'mappings'"><span class="truncate" :title="row.path">{{ row.path }}</span><span>{{ size(row.rss) }} / {{ size(row.pss) }}</span><span>{{ size(row.private) }}</span></template><template v-else-if="detailTab === 'files'"><span class="truncate" :title="row.path">{{ row.path }}</span><span>{{ row.fd }} / {{ row.mode || '—' }}</span><span>{{ number(row.position) }}</span></template><template v-else><span>{{ row.operation }} / {{ row.tid }}</span><span>{{ size(row.bytes_count) }} / {{ row.duration_seconds == null ? '—' : number(row.duration_seconds * 1000) + 'ms' }}</span><span class="truncate" :title="row.path">{{ row.path }}</span></template></div></template></VirtualList><p v-if="detailTab === 'files' || detailTab === 'trace'" class="muted footnote">打开文件是当前快照；Linux 跟踪仅覆盖开启后的系统调用，依赖 strace 和调试权限，存在额外开销。</p></template>
    </section></div>
    <div v-if="stopping" class="modal-backdrop"><section class="modal small" role="alertdialog" aria-modal="true"><h2>{{ stopping.force ? '强制结束进程？' : '结束进程？' }}</h2><p>{{ stopping.name }} · PID {{ stopping.key[0] }}</p><p>{{ stopping.force ? '强制结束不会等待程序正常退出，未保存的数据可能丢失。' : '程序可能有未保存的数据。请确认已经完成保存。' }}</p><div class="modal-actions"><button :disabled="working" @click="stopping = null">取消</button><button class="danger" :disabled="working" @click="stop">{{ working ? '正在结束…' : stopping.force ? '确认强制结束' : '确认结束' }}</button></div></section></div></Teleport>
  </div>
</template>

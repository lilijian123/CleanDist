<script setup>
import { computed, ref } from 'vue'
import { call, size } from './api'
import ClickSelect from './ClickSelect.vue'
const props = defineProps({ memory: Object, boot: Object, state: Object })
const emit = defineEmits(['refresh', 'error'])
const cpu = ref(''), memoryMb = ref(''), hard = ref(false), device = ref(''), readMb = ref(''), writeMb = ref('')
const pending = ref(null), busy = ref(false), metric = ref('cpu_percent')
const selected = computed(() => props.memory?.selected || {})
const caps = computed(() => props.state?.capabilities || {})
const same = (a, b) => a && b && a[0] === b[0] && a[1] === b[1]
const entry = computed(() => props.state?.managed?.find(item => same(item.key, selected.value.key)))
const events = computed(() => (props.state?.events || []).filter(item => same(item.key, selected.value.key)))
const managedReport = computed(() => !!entry.value?.report && (!selected.value.report || entry.value.report.samples > selected.value.report.samples))
const report = computed(() => (managedReport.value ? entry.value.report : selected.value.report) || {})
const options = { cpu_percent: 'CPU · 整机百分比', rss: '选中进程 RSS', controlled_memory: '受限口径内存', controlled_read_bps: '受管组指定设备读取 / 秒', controlled_write_bps: '受管组指定设备写入 / 秒', read_bps: '进程读取字节速率', write_bps: '进程写入字节速率', read_iops: '读取次数 / 秒', write_iops: '写入次数 / 秒', read_chars_bps: 'Linux 逻辑读取 / 秒', write_chars_bps: 'Linux 逻辑写入 / 秒' }
const points = computed(() => {
  if (metric.value === 'controlled_memory') return entry.value?.controlled_memory || []
  if (metric.value.startsWith('controlled_')) return entry.value?.controlled_io?.[metric.value.slice(11)] || []
  const offset = (managedReport.value ? entry.value.report_time_offset : selected.value.report_time_offset) || 0
  return (report.value.curves?.[metric.value] || []).map(p => [p[0] + offset, p[1]])
})
const chart = computed(() => {
  const values = points.value, valid = values.filter(p => p[1] != null && Number.isFinite(p[1]))
  if (!valid.length) return null
  const start = values[0][0], end = values.at(-1)[0], span = end - start || 1
  const relevant = events.value.filter(e => e.timestamp <= end)
  const limitFor = e => metric.value === 'cpu_percent' ? (e.limits?.cpu_cores == null ? null : e.limits.cpu_cores * 100 / (caps.value.logical_cpus || 1)) : metric.value === 'controlled_memory' ? e.limits?.memory_bytes : metric.value === 'controlled_read_bps' && e.limits?.device === entry.value?.controlled_io?.device ? e.limits?.read_bps : metric.value === 'controlled_write_bps' && e.limits?.device === entry.value?.controlled_io?.device ? e.limits?.write_bps : null
  const limits = relevant.map(limitFor).filter(v => v != null)
  const max = Math.max(...valid.map(p => p[1]), ...limits, 1) * 1.1
  const x = stamp => 10 + Math.max(0, Math.min(1, (stamp - start) / span)) * 580
  const y = value => 150 - value / max * 130
  let path = '', connected = false
  for (const [stamp, value] of values) {
    if (value == null) { connected = false; continue }
    path += `${connected ? 'L' : 'M'}${x(stamp)},${y(value)} `; connected = true
  }
  const bands = relevant.filter(e => e.action !== 'clear').map(e => {
    const next = events.value.find(other => other.timestamp > e.timestamp)
    return { x: x(e.timestamp), width: x(next?.timestamp ?? end) - x(e.timestamp), y: limitFor(e) == null ? null : y(limitFor(e)) }
  }).filter(b => b.width > 0)
  return { path, bands, start, end }
})
function preview() {
  const limit = { key: [...selected.value.key], cpu_cores: cpu.value === '' ? null : Number(cpu.value), memory_bytes: memoryMb.value === '' ? null : Math.round(Number(memoryMb.value) * 1024 ** 2), hard_memory: hard.value, device: device.value || null, read_bps: readMb.value === '' ? null : Math.round(Number(readMb.value) * 1024 ** 2), write_bps: writeMb.value === '' ? null : Math.round(Number(writeMb.value) * 1024 ** 2) }
  pending.value = { action: 'apply', name: selected.value.name, ...limit }
}
async function execute() {
  busy.value = true
  try {
    const { action, name, ...payload } = pending.value
    await call(action === 'apply' ? 'process_limit_apply' : 'process_limit_clear', { ...payload, confirmed: true })
    pending.value = null; emit('refresh')
  } catch (error) { emit('error', error.message) }
  finally { busy.value = false }
}
async function trace() {
  try { await call('process_trace', { enabled: !selected.value.trace?.active }); emit('refresh') }
  catch (error) { emit('error', error.message) }
}
const time = stamp => new Date(stamp * 1000).toLocaleTimeString('zh-CN')
</script>
<template>
  <section class="panel governance">
    <details>
      <summary>资源管控与文件归因 <span v-if="entry?.active">· 已应用限制</span></summary>
      <p v-if="!selected.key" class="muted">选择一个进程后设置限制和查看存储趋势。</p>
      <template v-else>
        <p><b>{{ selected.name }}</b> · PID {{ selected.key[0] }}</p>
        <p class="muted">{{ caps.scope }}</p>
        <div class="governance-fields">
          <label>CPU 核心当量<input v-model="cpu" type="number" min="0.01" step="0.1" :max="caps.logical_cpus" :disabled="!caps.cpu" placeholder="留空不限制"></label>
          <label>内存 MB<input v-model="memoryMb" type="number" min="1" :disabled="!caps.memory" placeholder="留空不限制"></label>
        </div>
        <label class="hard-limit"><input v-model="hard" type="checkbox" :disabled="!caps.memory">主动选择硬内存上限</label>
        <p class="muted">{{ caps.memory_label }}。{{ caps.memory_soft ? '默认 memory.high 仅施加回收压力。硬上限 memory.max 可能触发组内 OOM。' : 'Windows 只提供提交内存硬上限，可能导致分配失败，须主动勾选。' }}</p>
        <div v-if="caps.io" class="governance-fields">
          <label>真实块设备<input v-model="device" placeholder="major:minor，例如 8:0"></label>
          <label>读取 MB/秒<input v-model="readMb" type="number" min="0" step="0.1"></label>
          <label>写入 MB/秒<input v-model="writeMb" type="number" min="0" step="0.1"></label>
        </div>
        <p class="muted">{{ caps.io_label }}</p>
        <p class="muted">每次应用替换本应用原有限制，留空字段清除该项。{{ caps.clear_note }}</p>
        <div class="row gap"><button :disabled="busy || !(caps.cpu || caps.memory || caps.io)" @click="preview">预览并应用…</button><button :disabled="busy || !entry?.active" @click="pending = {action:'clear',key:[...selected.key],name:selected.name}">解除限制…</button><button :disabled="busy" @click="trace">{{ selected.trace?.active ? '停止文件跟踪' : '开启可选文件跟踪' }}</button></div>
        <details v-if="caps.reason"><summary>能力与权限说明</summary><p>{{ caps.reason }}</p><code v-if="caps.root">{{ caps.root }}</code></details>
        <p v-if="entry?.error" class="inline-note" role="status">{{ entry.error }}</p>
        <label>观察趋势<ClickSelect v-model="metric" label="观察趋势" :options="Object.entries(options).map(([value,label]) => ({value,label}))"/></label>
        <p class="muted">{{ metric === 'controlled_memory' ? caps.memory_label : metric.startsWith('controlled_') ? `受管组设备 ${entry?.controlled_io?.device || '未知'} 的 io.stat 存储速率；不等同进程逻辑读写，首个速率样本未知。` : '曲线只代表选中进程；CPU 上限可能作用于整个组，RSS 与提交量或 cgroup 内存不是同一指标。' }} 阴影区域为本应用限制生效区间；虚线为该指标的设置值。未知采样保留断点。</p>
        <div class="chart"><svg v-if="chart" viewBox="0 0 600 170" role="img" :aria-label="options[metric]"><g v-for="(band,i) in chart.bands" :key="i"><rect :x="band.x" y="10" :width="band.width" height="145" fill="var(--accent,#3977e8)" opacity=".08"/><path v-if="band.y != null" :d="`M${band.x},${band.y}h${band.width}`" stroke="var(--accent,#3977e8)" stroke-dasharray="5 4" fill="none"/></g><path class="chart-grid" d="M10 20H590 M10 85H590 M10 150H590"/><path class="chart-line" :d="chart.path"/></svg><div v-else class="empty-state">等待有效采样</div></div>
        <div v-if="chart" class="row"><small>{{ time(chart.start) }}</small><small>{{ time(chart.end) }}</small></div>
        <div class="governance-events"><p v-for="(event,i) in events.slice(-10)" :key="i"><small>{{ time(event.timestamp) }} · {{ {apply:'应用',modify:'修改',clear:'解除'}[event.action] }} <template v-if="event.limits">CPU {{ event.limits.cpu_cores ?? '不限' }} 核 · 内存 {{ size(event.limits.memory_bytes) }}</template></small></p></div>
        <details v-if="selected.trace"><summary>文件跟踪 · {{ selected.trace.active ? '采集中' : '已停止 / 不可用' }}</summary><p>{{ selected.trace.status }}</p><p class="muted">跟踪元数据不读取文件内容。路径未知时不推断；已完成的删除无法事后回收。</p><div class="trace-events"><p v-for="(event,i) in (selected.trace.events || []).slice(-40).reverse()" :key="i"><small>{{ event.operation }} · {{ event.path || '路径未知' }} · {{ event.bytes_count == null ? '字节未知' : size(event.bytes_count) }} · {{ event.success === false ? '失败' : event.success === true ? '成功' : '完成状态未知' }}</small></p></div></details>
      </template>
    </details>
    <Teleport to="body"><div v-if="pending" class="modal-backdrop" @click.self="!busy && (pending = null)"><section class="modal" role="dialog" aria-modal="true" aria-label="确认资源限制"><h2>{{ pending.action === 'apply' ? '确认应用资源限制' : '确认解除限制' }}</h2><p>{{ pending.name }} · PID {{ pending.key[0] }} · 启动身份 {{ pending.key[1] }}</p><p>{{ caps.scope }}</p><template v-if="pending.action === 'apply'"><p>CPU {{ pending.cpu_cores ?? '不限' }} 核 · 内存 {{ size(pending.memory_bytes) }} · {{ pending.hard_memory ? '硬限制，可能导致分配失败或 OOM' : '压力阈值（平台支持时）' }}</p><p v-if="pending.device">设备 {{ pending.device }} · 读 {{ size(pending.read_bps) }}/秒 · 写 {{ size(pending.write_bps) }}/秒</p><p>留空项会清除本应用此前的该项限制。</p></template><p v-else>{{ caps.clear_note }}</p><div class="row gap"><button :disabled="busy" @click="pending = null">取消</button><button :disabled="busy" @click="execute">{{ busy ? '处理中…' : '确认' }}</button></div></section></div></Teleport>
  </section>
</template>
<style scoped>
.governance { padding: 18px; margin-top: 16px; min-width: 0; }
.governance summary { cursor: pointer; font-weight: 600; padding: 5px 0; }
.governance p { overflow-wrap: anywhere; }
.governance-fields { display: flex; flex-wrap: wrap; gap: 12px; margin: 12px 0; }
.governance label { display: flex; flex-direction: column; gap: 7px; font-size: 13px; }
.governance .hard-limit { flex-direction: row; align-items: center; }
.hard-limit input { width: auto; }
.governance input, .governance select { max-width: 100%; }
.governance .row { display: flex; flex-wrap: wrap; justify-content: space-between; }
.governance .gap { justify-content: flex-start; gap: 8px; }
.trace-events { max-height: 240px; overflow: auto; }
.governance-events p { margin: 4px 0; }
.governance code { overflow-wrap: anywhere; }
</style>

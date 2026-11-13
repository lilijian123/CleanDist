<script setup>
import { computed, ref, watch } from 'vue'
import { call, number } from './api'
import ClickSelect from './ClickSelect.vue'
import VirtualList from './VirtualList.vue'

const props = defineProps({ processKey: Array, trace: Object, files: Array })
const emit = defineEmits(['refresh', 'file-access'])
const device = ref(''), busy = ref(false), feedback = ref('')
const mode = ref('process'), path = ref('')
const state = computed(() => props.trace || {})
const devices = computed(() => Object.entries(state.value.devices || {}).map(([value, count]) => ({ value, label: `${value} · ${number(count)} 条` })))
const rows = computed(() => state.value.events || [])
const load = async ({ offset, limit }) => ({ total: rows.value.length, rows: rows.value.slice(offset, offset + limit) })
watch(devices, list => { if (!list.some(item => item.value === device.value)) device.value = list[0]?.value || '' }, { immediate: true })
watch(() => state.value.path, value => {
  if (value) { path.value = value; mode.value = 'file' }
}, { immediate: true })

async function pickFile() {
  try {
    const file = await call('pick_trace_file')
    if (file) path.value = file
  } catch (error) {
    feedback.value = error.message
  }
}

async function action(exporting = false) {
  busy.value = true
  feedback.value = ''
  try {
    const result = exporting
      ? await call('block_trace_export', { key: props.processKey, device: device.value })
      : await call('block_trace', {
          key: props.processKey,
          enabled: !state.value.active,
          path: !state.value.active && mode.value === 'file' ? path.value : null,
        })
    if (exporting && result) feedback.value = `已保存 ${number(result.count)} 条请求：${result.path}`
    emit('refresh')
  } catch (error) {
    feedback.value = error.message
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="row gap wrap">
    <ClickSelect v-model="mode" label="采集范围" :disabled="busy || state.active"
      :options="[{value:'process', label:'进程树'}, {value:'file', label:'指定文件（ext4）'}]"/>
    <template v-if="mode === 'file'">
      <ClickSelect v-if="files?.length" v-model="path" label="进程关联文件" :disabled="busy || state.active"
        :options="files.map(value => ({ value, label: value.split('/').pop() }))"/>
      <button :disabled="busy || state.active" @click="pickFile">浏览文件…</button>
      <span v-if="path" class="truncate muted target-file" :title="path">{{ path }}</span>
    </template>
  </div>
  <div class="row gap wrap">
    <button class="primary" :disabled="busy || state.stopping || (!state.active && mode === 'file' && !path)" @click="action()">{{ state.stopping ? '正在整理…' : state.active ? '停止采集' : '开始采集' }}</button>
    <ClickSelect v-if="devices.length" v-model="device" label="导出设备" :options="devices"/>
    <button :disabled="busy || state.active || !device" title="MQSim File_Path · NANOSECOND" @click="action(true)">导出 MQSim .trace</button>
  </div>
  <p class="muted" role="status">{{ feedback || state.status || (mode === 'file' ? '先开始采集，再加载模型或查询数据库' : '采集窗口 · 30 秒') }}</p>
  <div v-if="state.count != null && !state.active" class="row gap wrap muted">
    <span>{{ number(state.reads ?? 0) }} 读 · {{ number(state.writes ?? 0) }} 写</span>
    <span v-if="state.path" class="truncate target-file" :title="state.path">文件匹配 · {{ state.path.split('/').pop() }}</span>
    <span v-else>进程树</span>
    <span v-if="state.partial">采集不完整</span>
    <span v-if="state.path && !state.count && state.status?.startsWith('本次')">缓存命中时可为 0 条</span>
    <button v-if="!state.path && !state.count && state.status?.startsWith('本次')"
      @click="emit('file-access')">查看文件调用 ↗</button>
  </div>
  <div v-if="rows.length" class="list-header block-grid"><span>相对时间</span><span>设备 / 线程</span><span>操作</span><span>起始扇区</span><span>扇区数</span></div>
  <VirtualList v-if="rows.length" :load="load" :revision="String(state.count) + ':' + rows.at(-1)?.[0]" empty="暂无块请求">
    <template #default="{row}"><div class="data-row block-grid"><span>{{ (row[0] / 1e6).toFixed(3) }} ms</span><span>{{ row[2] }} / {{ row[1] }}</span><span>{{ row[5] === 'R' ? '读' : '写' }}</span><span>{{ row[3] }}</span><span>{{ number(row[4]) }}</span></div></template>
  </VirtualList>
</template>

<style scoped>
.block-grid{display:grid;grid-template-columns:1fr 1.2fr .5fr 1.25fr .65fr;gap:10px}
.target-file{max-width:100%;font-size:12px}
</style>

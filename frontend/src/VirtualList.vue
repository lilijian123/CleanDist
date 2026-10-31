<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
const props = defineProps({ load: { type: Function, required: true }, revision: { default: '' }, rowKey: { default: '' }, rowHeight: { default: 56 }, empty: { default: '暂无记录' } })
const emit = defineEmits(['total', 'error', 'visible'])
const viewport = ref(), height = ref(400), top = ref(0), total = ref(0), version = ref(0), loading = ref(false), error = ref('')
const pages = new Map(), pending = new Map(), PAGE = 100, OVERSCAN = 5
let epoch = 0, observer, reloadId = 0
const start = computed(() => Math.max(0, Math.floor(top.value / props.rowHeight) - OVERSCAN))
const end = computed(() => Math.min(total.value, Math.ceil((top.value + height.value) / props.rowHeight) + OVERSCAN))
const visible = computed(() => {
  version.value
  return Array.from({ length: Math.max(0, end.value - start.value) }, (_, offset) => {
    const index = start.value + offset
    return { index, row: pages.get(Math.floor(index / PAGE))?.[index % PAGE] }
  })
})
watch(visible, items => emit('visible', items.filter(item => item.row).map(item => item.row)), { flush: 'post' })
async function fetchPage(page, current = epoch, refresh = false) {
  if ((!refresh && pages.has(page)) || pending.has(page)) return
  pending.set(page, current); loading.value = true
  try {
    const result = await props.load({ offset: page * PAGE, limit: PAGE })
    if (current !== epoch) return
    total.value = Number(result.total || 0); emit('total', total.value)
    pages.set(page, result.rows || [])
    // Keep at most eight pages in memory; row DOM remains bounded by viewport + overscan.
    while (pages.size > 8) pages.delete(pages.keys().next().value)
    version.value++; error.value = ''
  } catch (cause) { if (current === epoch) { error.value = cause.message; emit('error', cause.message) } }
  finally { if (pending.get(page) === current) pending.delete(page); loading.value = pending.size > 0 }
}
function ensure() {
  if (!total.value) return
  for (let page = Math.floor(start.value / PAGE); page <= Math.floor(Math.max(0, end.value - 1) / PAGE); page++) fetchPage(page)
}
async function reload(reset = true) {
  if (!reset && reloadId === epoch && reloadId !== 0) return
  const current = ++epoch
  reloadId = current
  pending.clear(); error.value = ''
  if (reset) { pages.clear(); version.value++; top.value = 0; if (viewport.value) viewport.value.scrollTop = 0 }
  const first = reset ? 0 : Math.floor(start.value / PAGE)
  const last = reset ? 0 : Math.max(first, Math.floor(Math.max(0, end.value - 1) / PAGE))
  try {
    // Periodic refresh keeps the rendered rows until their replacements arrive.
    await Promise.all(Array.from({ length: last - first + 1 }, (_, index) => fetchPage(first + index, current, !reset)))
    await nextTick(); ensure()
  } finally { if (reloadId === current) reloadId = 0 }
}
watch(() => props.revision, () => reload())
watch([start, end], ensure)
onMounted(() => {
  observer = new ResizeObserver(entries => { height.value = entries[0].contentRect.height; ensure() })
  observer.observe(viewport.value); reload()
})
onBeforeUnmount(() => { epoch++; observer?.disconnect() })
defineExpose({ reload })
</script>
<template>
  <div ref="viewport" class="virtual-list" tabindex="0" @scroll.passive="top = $event.target.scrollTop" :aria-busy="loading">
    <div v-if="!total" class="empty-state">{{ error || (loading ? '正在加载…' : empty) }}<button v-if="error" @click="reload()">重试</button></div>
    <div class="virtual-space" :style="{ height: total * rowHeight + 'px' }">
      <div v-for="item in visible" :key="rowKey && item.row?.[rowKey] != null ? item.row[rowKey] : item.index" class="virtual-row" :style="{ height: rowHeight + 'px', transform: `translateY(${item.index * rowHeight}px)` }">
        <slot v-if="item.row" :row="item.row" :index="item.index"/><div v-else class="row-skeleton">正在加载…</div>
      </div>
    </div>
  </div>
</template>

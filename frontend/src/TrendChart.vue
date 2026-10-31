<script setup>
import { computed } from 'vue'
const props = defineProps({ points: { type: Array, default: () => [] }, label: String })
const samples = computed(() => props.points.map((p, i) => Array.isArray(p) ? [p[0], p[1]] : [i, p]).filter(p => p[1] != null && Number.isFinite(Number(p[1]))))
const path = computed(() => {
  const points = samples.value
  if (!points.length) return ''
  const minX = points[0][0], maxX = points.at(-1)[0], values = points.map(p => Number(p[1]))
  const min = Math.min(...values), max = Math.max(...values), span = max - min || max || 1
  return points.map((p, i) => `${i ? 'L' : 'M'}${12 + (p[0] - minX) / (maxX - minX || 1) * 576},${150 - (p[1] - min) / span * 130}`).join(' ')
})
</script>
<template><div class="chart"><svg v-if="samples.length" viewBox="0 0 600 170" role="img" :aria-label="label || '资源趋势'"><path class="chart-grid" d="M12 20H588 M12 85H588 M12 150H588"/><path class="chart-line" :d="path"/></svg><div v-else class="empty-state">等待有效采样</div></div></template>

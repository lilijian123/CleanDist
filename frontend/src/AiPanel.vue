<script setup>
import { computed, ref } from 'vue'
import { call } from './api'
const props = defineProps({ state: { type: Object, default: () => ({}) }, file: Object, hasKey: Boolean, selected: Boolean, automatic: Boolean })
const emit = defineEmits(['settings', 'error'])
const sending = ref(false)
const busy = computed(() => sending.value || props.state.busy)
async function request(mode) {
  sending.value = true
  try { await call('ai', { mode, ...(props.file ? { file: props.file } : {}) }) }
  catch (error) { emit('error', error.message) }
  finally { sending.value = false }
}
async function cancel() { try { await call('ai_cancel') } catch (error) { emit('error', error.message) } }
</script>
<template>
  <section class="ai-panel panel">
    <div class="section-heading"><div class="row gap"><span class="ai-mark">✦</span><h2>AI 助手</h2></div><span class="muted">DeepSeek</span></div>
    <p v-if="!hasKey" class="muted">配置 API Key 后，可根据当前摘要生成分析。</p>
    <div class="row gap wrap"><button v-if="!hasKey" @click="emit('settings')">配置 AI</button><template v-else><button v-if="!automatic" :disabled="busy || !selected" @click="request(file ? 'cleanup' : 'analyze')">✦ {{ file ? '清理建议' : '分析当前进程' }}</button><button v-if="!file" :disabled="busy || !selected" @click="request('predict')">趋势预测</button><button v-if="busy" @click="cancel">停止</button></template></div>
    <div class="ai-output" aria-live="polite"><p v-if="state.target" class="muted ai-target">{{ typeof state.target === 'string' ? state.target : '当前分析对象' }}</p><pre v-if="state.text">{{ state.text }}</pre><p v-else class="muted">{{ busy ? '正在分析…' : '选择对象，查看基于采样或扫描结果的建议。' }}</p><p v-if="state.error" class="error-text">{{ state.error }}</p></div>
    <details class="quiet-details"><summary>发送给 AI 的信息</summary><p>发送进程指标或文件元数据摘要。不会发送文件内容；回答仅供判断参考。</p></details>
  </section>
</template>

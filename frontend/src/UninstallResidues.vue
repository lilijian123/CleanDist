<script setup>
import { computed, ref, watch } from 'vue'
import { call, size } from './api'

const props = defineProps({ residues: Object })
const emit = defineEmits(['updated', 'refresh', 'error'])
const selected = ref([])
const busy = ref(false)
const receipt = ref(null)
const limit = ref(50)
const items = computed(() => props.residues?.items || [])

function selectAll(event) {
  selected.value = event.target.checked ? items.value.map(item => item.id) : []
}

watch(() => props.residues?.token, () => {
  selected.value = []
  receipt.value = null
  limit.value = 50
})

async function recycle() {
  busy.value = true
  try {
    receipt.value = await call('deps_residue_recycle', {
      token: props.residues.token,
      ids: selected.value,
      confirmed: true,
    })
    selected.value = []
    emit('updated', receipt.value.residues)
    emit('refresh')
  } catch (error) {
    emit('error', error.message)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section v-if="residues || receipt" class="residues">
    <div class="section-heading">
      <h3>卸载后残留<span v-if="residues?.items.length"> · {{ residues.items.length }} 项</span></h3>
      <button v-if="residues?.items.length" :disabled="busy || !selected.length" @click="recycle">
        {{ busy ? '正在回收…' : '回收所选' }}<span v-if="selected.length">（{{ selected.length }}）</span>
      </button>
    </div>
    <label v-if="items.length" class="select-all">
      <input type="checkbox" :checked="selected.length === items.length"
        :indeterminate="selected.length > 0 && selected.length < items.length" :disabled="busy" @change="selectAll">
      全选 {{ items.length }} 项
    </label>
    <label v-for="item in items.slice(0, limit)" :key="item.id" class="residue">
      <input v-model="selected" type="checkbox" :value="item.id" :disabled="busy">
      <span><b>{{ item.path }}</b><small>{{ item.owner }} · {{ item.evidence }}</small></span>
      <small>{{ size(item.bytes) }}</small>
    </label>
    <button v-if="items.length > limit" @click="limit += 50">显示更多（剩余 {{ items.length - limit }} 项）</button>
    <p v-if="residues && residues.complete !== false && !items.length && !receipt" class="muted">未发现可回收的已确认残留。</p>
    <p v-for="note in residues?.notes" :key="note" class="muted">{{ note }}</p>
    <p v-if="receipt" role="status">{{ receipt.message }}</p>
    <p v-for="item in receipt?.results.filter(row => row.error)" :key="item.candidate_id" class="error-text">{{ item.path }} · {{ item.error }}</p>
  </section>
</template>

<style scoped>
.residues { border-top: 1px solid var(--line); padding-top: 14px; }
.select-all { display: flex; align-items: center; gap: 12px; margin-top: 8px; }
.residue { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 12px; padding: 10px 0; }
.residue span { min-width: 0; overflow-wrap: anywhere; }
.residue small { display: block; color: var(--muted); }
</style>

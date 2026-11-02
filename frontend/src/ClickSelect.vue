<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
const props = defineProps({ modelValue: { default: '' }, options: { type: Array, default: () => [] }, label: String, disabled: Boolean })
const emit = defineEmits(['update:modelValue'])
const open = ref(false), root = ref()
const items = computed(() => props.options.map(item => typeof item === 'object' ? item : { value: item, label: item }))
const caption = computed(() => items.value.find(item => item.value === props.modelValue)?.label || props.label || '请选择')
function outside(event) { if (!root.value?.contains(event.target)) open.value = false }
onMounted(() => document.addEventListener('pointerdown', outside))
onBeforeUnmount(() => document.removeEventListener('pointerdown', outside))
</script>
<template>
  <div ref="root" class="select" @keydown.esc="open = false">
    <button class="select-button" :disabled="disabled" :aria-label="label" aria-haspopup="listbox" :aria-expanded="open && !disabled" @click="open = !open" @keydown.down.prevent="open = true">{{ caption }} <span>⌄</span></button>
    <div v-if="open && !disabled" role="listbox" class="select-menu">
      <button v-for="item in items" :key="item.value" role="option" :aria-selected="item.value === modelValue" @click="emit('update:modelValue', item.value); open = false">{{ item.label }}<span v-if="item.value === modelValue">✓</span></button>
    </div>
  </div>
</template>

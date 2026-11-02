<script setup>
import { computed, ref } from 'vue'
import FileWorkspace from './FileWorkspace.vue'
const props = defineProps({ files: Object, boot: Object, ai: Object, directories: Object })
const emit = defineEmits(['error', 'settings', 'refresh'])
const tool = ref('cleanup')
const tools = [['cleanup','分类清理'],['large','大文件清理'],['duplicates','重复文件清理'],['matrix','空间矩阵分析'],['quota','目录配额'],['vault','应用回收站']]
const files = computed(() => props.files?.workspaces?.[tool.value] || props.files || {})
function selectTool(value) { tool.value = value; document.querySelector('.main-content')?.scrollTo(0,0) }
</script>
<template>
  <div class="page file-page">
    <nav class="panel file-nav" aria-label="文件清理工具"><button v-for="[value,label] in tools" :key="value" :class="{active:tool === value}" :aria-current="tool === value ? 'page' : undefined" @click="selectTool(value)">{{ label }}</button></nav>
    <KeepAlive><FileWorkspace :key="tool" :tool="tool" :title="tools.find(([value]) => value === tool)?.[1]" :files="files" :directories="directories" :ai="ai" :boot="boot" @error="emit('error',$event)" @settings="emit('settings')" @refresh="emit('refresh')"/></KeepAlive>
  </div>
</template>

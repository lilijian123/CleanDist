<script setup>
import { computed, ref, watch } from 'vue'
import { call, number } from './api'
import VirtualList from './VirtualList.vue'
import ClickSelect from './ClickSelect.vue'
import DependencyPanel from './DependencyPanel.vue'
const props = defineProps({ state: Object, boot: Object, dependencies: Object })
const emit = defineEmits(['error', 'refresh'])
const view = ref('scan')
const query = ref(''), source = ref(''), roots = ref(null), menu = ref(false), list = ref(), count = ref(0), starting = ref(false)
const options = computed(() => [{ value: '', label: '全部类型' }, ...(props.boot.app_sources || []).map(value => ({ value, label: value }))])
const load = page => call('app_rows', { ...page, query: query.value, source: source.value })
async function scan() { starting.value = true; try { await call('apps_scan', { roots: roots.value }); emit('refresh') } catch (error) { emit('error', error.message) } finally { starting.value = false } }
async function folder() { try { const value = await call('pick_folder'); const path = typeof value === 'string' ? value : value?.path; if (path) roots.value = [...new Set([...(roots.value || []), path])] } catch (error) { emit('error', error.message) } }
function toggle(root) { if (roots.value === null) roots.value = [...props.boot.disks]; roots.value = roots.value.includes(root) ? roots.value.filter(value => value !== root) : [...roots.value, root] }
watch(() => props.state.busy, (busy, before) => { if (before && !busy) list.value?.reload() })
watch(() => props.state.count, () => { if (!props.state.busy) list.value?.reload() })
</script>
<template>
  <div class="page apps-page">
    <div class="row gap"><button :class="{active:view === 'scan'}" @click="view = 'scan'">应用清单</button><button :class="{active:view === 'dependencies'}" @click="view = 'dependencies'">依赖与孤儿清理</button></div>
    <DependencyPanel v-if="view === 'dependencies'" :state="dependencies || {}" :boot="boot" @error="emit('error',$event)" @refresh="emit('refresh')"/>
    <template v-else>
    <div class="page-heading"><div><h1>应用扫描</h1><p class="muted">查看系统登记应用与便携程序候选。</p></div><div class="row gap"><div v-if="boot.can_disk_scan" class="select"><button @click="menu = !menu">{{ roots === null ? '全部磁盘' : '已选 ' + roots.length + ' 个位置' }} ⌄</button><div v-if="menu" class="select-menu disk-menu"><button @click="roots = null">全部磁盘</button><label v-for="disk in boot.disks" :key="disk"><input type="checkbox" :checked="roots === null || roots.includes(disk)" @change="toggle(disk)">{{ disk }}</label><button @click="menu = false">完成</button></div></div><button @click="folder">添加应用目录</button><button v-if="roots !== null" @click="roots = null">恢复全部</button><button class="primary" :disabled="starting || state.busy || (roots !== null && !roots.length)" @click="scan">{{ state.busy || starting ? '正在扫描…' : '开始扫描' }}</button></div></div>
    <div class="metrics apps-metrics"><div class="metric panel"><span>已发现应用</span><div><strong>{{ number(state.count) }}</strong><small>个</small></div></div><div class="metric panel"><span>当前筛选</span><div><strong>{{ number(count) }}</strong><small>个</small></div></div></div>
    <section class="panel apps-content"><div class="toolbar"><ClickSelect v-model="source" :options="options" label="应用类型"/><input v-model="query" placeholder="搜索名称、发布者或位置" aria-label="搜索应用"/></div><div class="list-header apps-grid"><span>应用名称</span><span>类型</span><span>版本</span><span>发布者</span><span>位置</span></div><VirtualList ref="list" :load="load" :revision="query + '|' + source" @total="count = $event" @error="emit('error',$event)" empty="点击开始扫描，查看已登记应用"><template #default="{row}"><div class="data-row apps-grid"><b class="truncate" :title="row.name">{{ row.name }}</b><span class="truncate">{{ row.source }}</span><span class="truncate">{{ row.version || '—' }}</span><span class="truncate" :title="row.publisher">{{ row.publisher || '—' }}</span><span class="truncate" :title="row.path">{{ row.path || '未定位' }}</span></div></template></VirtualList></section>
    <p class="muted">{{ state.stage }} · 用时 {{ number(state.elapsed) }} 秒 · 跳过 {{ number(state.skipped) }} 项</p><details v-if="state.errors?.length" class="quiet-details"><summary>扫描提示（{{ state.errors.length }}）</summary><p v-for="(message,index) in state.errors" :key="index">{{ message }}</p></details><p v-if="state.error" class="error-text">{{ state.error }}</p>
    </template>
  </div>
</template>

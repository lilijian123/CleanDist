<script setup>
import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
import { call } from './api'
import ClickSelect from './ClickSelect.vue'
import MemoryPage from './MemoryPage.vue'
import FilePage from './FilePage.vue'
import AppsPage from './AppsPage.vue'
const page = ref('memory'), boot = ref(null), state = ref({ files: {}, memory: {}, apps: {}, ai: {}, demo: {}, operation: {}, control: {}, directories: {}, dependencies: {} }), error = ref(''), startup = ref('正在连接桌面服务…'), memory = ref(), secret = ref(''), model = ref(''), theme = ref('dark'), saving = ref(false), saved = ref(''), demoBusy = ref(false), settingTab = ref('appearance')
let timer, polling = false, stopped = false, processTicks = 0
const status = computed(() => state.value.operation?.busy ? state.value.operation.message || '正在处理…' : state.value.files?.operation?.busy ? state.value.files.operation.message || '正在回收…' : '')
function fail(message) { error.value = message }
function setTheme(value) { theme.value = value; document.documentElement.dataset.theme = value }
async function refresh() {
  if (polling || !boot.value || stopped) return
  polling = true
  try { state.value = await call('state'); if (page.value === 'memory' && ++processTicks % 2 === 0) memory.value?.refresh() }
  catch (cause) { fail(cause.message) }
  finally { polling = false }
}
async function start() {
  startup.value = '正在连接桌面服务…'
  try { boot.value = await call('bootstrap'); model.value = boot.value.model; if (boot.value.settings_error) fail(boot.value.settings_error); setTheme(boot.value.theme === 'light' ? 'light' : 'dark'); await refresh(); timer = setInterval(refresh, 1000) }
  catch (cause) { startup.value = cause.message }
}
async function save() {
  saving.value = true; saved.value = ''
  const key = secret.value.trim(); secret.value = ''
  try { const result = await call('settings_save', { theme: theme.value, model: model.value, ...(key ? { key } : {}) }); boot.value = { ...boot.value, theme: theme.value, model: model.value, has_key: result?.has_key ?? (key ? true : boot.value.has_key) }; saved.value = '设置已保存' }
  catch (cause) { fail(cause.message) }
  finally { saving.value = false }
}
async function clearKey() { saving.value = true; try { await call('settings_clear_key'); secret.value = ''; boot.value.has_key = false; saved.value = 'API Key 已移除' } catch (cause) { fail(cause.message) } finally { saving.value = false } }
async function demo() { demoBusy.value = true; try { await call('demo', { enabled: !state.value.demo?.running }); await refresh() } catch (cause) { fail(cause.message) } finally { demoBusy.value = false } }
function showSettings() { page.value = 'settings' }
onMounted(start)
onBeforeUnmount(() => { stopped = true; clearInterval(timer); secret.value = '' })
</script>
<template>
  <div class="app-shell">
    <header class="app-header"><div class="brand"><span class="brand-mark">C<span>·</span></span><div><strong>CleanDist</strong><small>资源观察与清理</small></div></div><div class="row gap"><span v-if="boot" class="platform-badge">{{ boot.platform }}</span><button v-if="boot" :disabled="demoBusy" @click="demo">{{ state.demo?.running ? '停止演示' : '体验演示' }}</button><button v-if="boot" class="icon-button" :class="{active:page === 'settings'}" @click="page = page === 'settings' ? 'memory' : 'settings'" aria-label="设置">⚙</button></div></header>
    <template v-if="boot"><nav class="main-nav" aria-label="主导航"><button v-for="[value,label,index] in [['memory','内存监测','01'],['files','文件清理','02'],['apps','应用扫描','03']]" :key="value" :class="{active:page === value}" @click="page = value"><small>{{ index }}</small>{{ label }}</button></nav>
      <div v-if="error" class="error-banner" role="alert"><span>{{ error }}</span><button @click="error = ''" aria-label="关闭提示">✕</button></div>
      <div v-if="status" class="operation-banner" role="status">{{ status }}</div>
      <main class="main-content"><MemoryPage v-if="page === 'memory'" ref="memory" :memory="state.memory" :control="state.control" :ai="state.ai" :boot="boot" @error="fail" @settings="showSettings" @refresh="refresh"/><KeepAlive><FilePage v-if="page === 'files'" :files="state.files" :directories="state.directories" :ai="state.ai" :boot="boot" @error="fail" @settings="showSettings" @refresh="refresh"/></KeepAlive><AppsPage v-if="page === 'apps'" :state="state.apps" :dependencies="state.dependencies" :boot="boot" @error="fail" @refresh="refresh"/>
      <div v-if="page === 'settings'" class="page settings-page"><div class="page-heading"><div><h1>设置</h1><p class="muted">外观与 AI 连接</p></div><button @click="page = 'memory'">返回</button></div><div class="settings-layout"><nav class="settings-nav" aria-label="设置分类"><button :class="{active:settingTab === 'appearance'}" @click="settingTab = 'appearance'">外观</button><button :class="{active:settingTab === 'ai'}" @click="settingTab = 'ai'">AI 配置</button></nav><div class="settings-body"><section v-if="settingTab === 'appearance'" class="panel settings-card"><div class="section-heading"><div><h2>外观</h2><p class="muted">统一应用到所有页面</p></div><ClickSelect :model-value="theme" @update:model-value="setTheme" label="主题" :options="[{value:'dark',label:'深色'},{value:'light',label:'浅色'}]"/></div></section><section v-else class="panel settings-card"><div class="section-heading"><h2>DeepSeek</h2><span class="status-dot" :class="{configured:boot.has_key}">{{ boot.has_key ? '已配置密钥' : '尚未配置' }}</span></div><label class="field"><span>模型</span><ClickSelect v-model="model" :options="boot.models" label="DeepSeek 模型"/></label><label class="field"><span>API Key</span><input v-model="secret" type="password" autocomplete="new-password" spellcheck="false" autocapitalize="off" :placeholder="boot.has_key ? '输入新密钥以替换；留空保留原密钥' : '输入 DeepSeek API Key'" @copy.prevent @cut.prevent @dragstart.prevent @contextmenu.prevent @keydown.ctrl.c.prevent @keydown.ctrl.x.prevent></label><p class="muted">密钥仅写入本机设置，不提供回读。输入后保存即清空此输入框。</p><div class="row gap"><button class="danger-quiet" :disabled="!boot.has_key || saving" @click="clearKey">移除已保存的 Key</button></div></section><div class="row gap"><button class="primary" :disabled="saving" @click="save">{{ saving ? '正在保存…' : '保存设置' }}</button><span class="success-text" role="status">{{ saved }}</span></div></div></div></div>
      </main><footer class="app-footer"><span>{{ status || '就绪' }}</span><span v-if="state.demo?.running" class="demo-status">演示运行中<span v-if="state.demo.path" :title="state.demo.path"> · {{ state.demo.path }}</span></span></footer>
    </template>
    <main v-else class="startup"><div class="brand-mark large">C<span>·</span></div><h1>CleanDist</h1><p>{{ startup }}</p><button @click="start">重新连接</button></main>
  </div>
</template>

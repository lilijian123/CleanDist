// A browser test can inject this before the app loads. Production never creates it.
let ready
export function connect() {
  if (window.__CLEANDIST_TEST_BRIDGE__) return Promise.resolve(window.__CLEANDIST_TEST_BRIDGE__)
  if (window.pywebview?.api?.call) return Promise.resolve(window.pywebview.api)
  if (!ready) ready = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('请通过桌面启动器打开 CleanDist。浏览器页面尚未连接 Python 服务。')), 6500)
    window.addEventListener('pywebviewready', () => { clearTimeout(timer); resolve(window.pywebview.api) }, { once: true })
  })
  return ready
}
export async function call(action, payload = {}) {
  const bridge = await connect()
  const result = await bridge.call(action, payload)
  if (!result || result.ok !== true) throw new Error(result?.error || '桌面服务返回无效响应')
  return result.data
}
export function size(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—'
  let number = Number(value), unit = 0
  while (Math.abs(number) >= 1024 && unit < 4) { number /= 1024; unit++ }
  return `${number.toLocaleString('zh-CN', { maximumFractionDigits: unit ? 1 : 0 })} ${['B', 'KB', 'MB', 'GB', 'TB'][unit]}`
}
export const metricSize = value => { const [number, unit] = size(value).split(' '); return { number, unit: unit || '' } }
export const number = value => value == null ? '—' : Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 1 })
export const percentage = value => value == null ? '—' : `${number(value)}%`
export const riskName = risk => ({ low: '低风险', medium: '中风险', high: '高风险' }[risk] || risk || '待核验')

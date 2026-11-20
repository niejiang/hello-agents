<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref } from 'vue'
import { renderMarkdown } from './markdown'

type ContentBlock = { type: string; text?: string; html?: string }
type Message = { role: 'user' | 'assistant'; content: ContentBlock[] }

const SUGGESTIONS = ['这个项目的入口在哪里？', '帮我解释一下项目结构', '查找某个方法的实现并说明调用方']
// 各平台沿用主流网页 AI 对话的习惯：Enter 发送；Mac 用 Option+Enter、Windows 用 Shift+Enter 换行，两种组合在两个平台都可用。
const isMac = /mac/i.test((navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData?.platform ?? navigator.platform)
const newlineKeys = isMac ? ['⌥ Option', 'Enter'] : ['Shift', 'Enter']

const project = ref('')
const session = ref('')
const activeProject = ref('')
const editingProject = ref(true)
const sidebarOpen = ref(false)
const input = ref('')
const messages = ref<Message[]>([])
const busy = ref(false)
const creating = ref(false)
const error = ref('')
const projectError = ref('')
const startedAt = ref(0)
const now = ref(0)
let ticker: number | undefined

const conversation = ref<HTMLElement | null>(null)
const composer = ref<HTMLTextAreaElement | null>(null)
const projectInput = ref<HTMLInputElement | null>(null)

const projectName = computed(() => activeProject.value.split('/').filter(Boolean).pop() ?? '')
const elapsed = computed(() => Math.max(0, Math.floor((now.value - startedAt.value) / 1000)))
const status = computed(() => busy.value ? '思考中' : session.value ? '就绪' : '未选择项目')

async function request(url: string, body: object) {
  let response: Response
  try {
    response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  } catch { throw new Error('无法连接后端服务，请确认服务已启动') }
  // 后端未启动时代理可能返回非 JSON 内容，此时给出可读提示。
  const data = await response.json().catch(() => null)
  if (!response.ok) {
    if (typeof data?.detail === 'string') throw new Error(data.detail)
    throw new Error(response.status >= 500 ? '后端服务不可用，请确认服务已启动' : '请求无效，请检查输入')
  }
  if (data === null) throw new Error('服务返回了无法识别的内容')
  return data
}

function focusComposer() {
  nextTick(() => composer.value?.focus())
}

function openProjectEditor() {
  editingProject.value = true
  sidebarOpen.value = true
  project.value = activeProject.value || project.value
  nextTick(() => { projectInput.value?.focus(); projectInput.value?.select() })
}

async function createSession() {
  if (!project.value.trim() || creating.value || busy.value) return
  creating.value = true; projectError.value = ''
  try {
    const data = await request('/api/sessions', { project_path: project.value })
    // 切换项目后释放旧会话，失败不影响新会话。
    if (session.value) fetch(`/api/sessions/${session.value}`, { method: 'DELETE' }).catch(() => {})
    session.value = data.session_id; activeProject.value = data.project_path; messages.value = []
    editingProject.value = false; sidebarOpen.value = false
    focusComposer()
  } catch (e) { projectError.value = e instanceof Error ? e.message : '无法创建会话' }
  finally { creating.value = false }
}

function scrollToBottom() {
  nextTick(() => conversation.value?.scrollTo({ top: conversation.value.scrollHeight, behavior: 'smooth' }))
}

async function send(preset?: string) {
  const text = (preset ?? input.value).trim()
  if (!text || busy.value) return
  if (!session.value) { projectError.value = '请先选择要分析的项目'; openProjectEditor(); return }
  busy.value = true; error.value = ''
  if (preset === undefined) input.value = ''
  nextTick(resizeComposer)
  messages.value.push({ role: 'user', content: [{ type: 'text', text }] })
  startedAt.value = now.value = Date.now()
  ticker = window.setInterval(() => { now.value = Date.now() }, 1000)
  scrollToBottom()
  try {
    const data = await request(`/api/sessions/${session.value}/messages`, { message: text })
    // 回复到达时渲染一次并缓存，避免输入时重复解析 Markdown。
    const content = (data.content as ContentBlock[]).map(block =>
      block.type === 'text' && block.text ? { ...block, html: renderMarkdown(block.text) } : block)
    messages.value.push({ role: 'assistant', content })
  } catch (e) {
    messages.value.pop()
    error.value = e instanceof Error ? e.message : '发送失败，请重试'
    if (!input.value) input.value = text
  } finally {
    window.clearInterval(ticker)
    busy.value = false
    scrollToBottom()
    focusComposer()
    nextTick(resizeComposer)
  }
}

function isComposing(event: KeyboardEvent) {
  // keyCode 229 兼容 Safari 中输入法选词结束时的回车事件。
  return event.isComposing || event.keyCode === 229
}

function onComposerKeydown(event: KeyboardEvent) {
  if (event.key !== 'Enter' || isComposing(event)) return
  if (event.shiftKey) return // 浏览器默认插入换行
  event.preventDefault()
  if (event.altKey) { insertNewline(); return }
  send()
}

function insertNewline() {
  const el = composer.value
  if (!el) return
  el.setRangeText('\n', el.selectionStart, el.selectionEnd, 'end')
  input.value = el.value
  resizeComposer()
}

function onProjectKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !isComposing(event)) { event.preventDefault(); createSession() }
  if (event.key === 'Escape' && session.value) editingProject.value = false
}

function resizeComposer() {
  const el = composer.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 220)}px`
}

async function copyText(button: HTMLElement, text: string) {
  const label = button.textContent
  try { await navigator.clipboard.writeText(text); button.textContent = '已复制' } catch { button.textContent = '复制失败' }
  window.setTimeout(() => { button.textContent = label }, 1500)
}

function onConversationClick(event: MouseEvent) {
  const button = (event.target as HTMLElement).closest<HTMLElement>('.copy-code')
  if (!button) return
  copyText(button, button.closest('.code-block')?.querySelector('code')?.textContent ?? '')
}

function copyMessage(event: MouseEvent, message: Message) {
  copyText(event.currentTarget as HTMLElement, message.content.map(block => block.text ?? '').join('\n\n'))
}

onBeforeUnmount(() => window.clearInterval(ticker))
</script>

<template>
  <div class="layout">
    <aside :class="['sidebar', { open: sidebarOpen }]">
      <div class="brand">
        <span class="brand-mark">C</span>
        <div class="brand-text"><strong>CodeChat</strong><small>代码分析助手</small></div>
        <button class="icon-button only-mobile" aria-label="关闭侧栏" @click="sidebarOpen = false">✕</button>
      </div>

      <section class="panel">
        <div class="panel-title">项目</div>
        <div v-if="session && !editingProject" class="project-card">
          <div class="project-icon">▤</div>
          <div class="project-meta">
            <strong :title="activeProject">{{ projectName }}</strong>
            <small :title="activeProject">{{ activeProject }}</small>
          </div>
          <button class="ghost-button" @click="openProjectEditor">切换</button>
        </div>
        <form v-else class="project-form" @submit.prevent="createSession">
          <input
            id="project" ref="projectInput" v-model="project" :disabled="creating" autocomplete="off" spellcheck="false"
            placeholder="输入本机项目的绝对路径" aria-label="项目路径" @keydown="onProjectKeydown"
          >
          <div class="form-actions">
            <button v-if="session" type="button" class="ghost-button" @click="editingProject = false">取消</button>
            <button type="submit" class="primary-button" :disabled="creating || !project.trim()">
              {{ creating ? '正在打开…' : session ? '切换并新建对话' : '打开项目' }}
            </button>
          </div>
          <p v-if="projectError" class="error" role="alert">{{ projectError }}</p>
          <p v-if="session" class="panel-note">切换后将开始新的对话，当前对话不会保留。</p>
        </form>
      </section>

      <p class="sidebar-foot">读取到的代码片段会发送给配置的模型服务，请只选择允许分析的项目。会话仅保留在服务运行期间。</p>
    </aside>
    <div v-if="sidebarOpen" class="scrim only-mobile" @click="sidebarOpen = false"></div>

    <main class="main">
      <header class="topbar">
        <button class="icon-button only-mobile" aria-label="打开侧栏" @click="sidebarOpen = true">☰</button>
        <div class="topbar-title">
          <strong>{{ projectName || '代码对话' }}</strong>
          <small v-if="activeProject" :title="activeProject">{{ activeProject }}</small>
        </div>
        <span :class="['status', { ready: session && !busy, busy }]"><i></i>{{ status }}</span>
      </header>

      <div ref="conversation" class="conversation" aria-live="polite" @click="onConversationClick">
        <div class="thread">
          <section v-if="!messages.length" class="welcome">
            <div class="welcome-mark">✳</div>
            <h1>{{ session ? `开始分析 ${projectName}` : '从一个代码问题开始' }}</h1>
            <p>{{ session ? '助手会先搜索和读取项目代码，再结合真实文件与行号回答。' : '先选择一个本地项目，再向助手提问。' }}</p>
            <button v-if="!session" class="primary-button" @click="openProjectEditor">选择项目</button>
            <div class="suggestions">
              <button v-for="item in SUGGESTIONS" :key="item" class="suggestion" :disabled="busy" @click="send(item)">
                <span>{{ item }}</span><span class="arrow">→</span>
              </button>
            </div>
          </section>

          <template v-for="(message, index) in messages" :key="index">
            <article v-if="message.role === 'user'" class="msg user">
              <div class="bubble">{{ message.content.map(block => block.text).join('\n') }}</div>
            </article>
            <article v-else class="msg assistant">
              <div class="avatar">C</div>
              <div class="msg-main">
                <template v-for="(block, i) in message.content" :key="i">
                  <div v-if="block.html" class="markdown" v-html="block.html"></div>
                  <div v-else-if="block.type === 'text'" class="markdown plain">{{ block.text }}</div>
                  <div v-else class="unsupported">暂不支持展示此内容类型。</div>
                </template>
                <div class="msg-actions"><button class="ghost-button small" @click="copyMessage($event, message)">复制</button></div>
              </div>
            </article>
          </template>

          <div v-if="busy" class="msg assistant typing">
            <div class="avatar">C</div>
            <div class="typing-body"><span class="dots"><i></i><i></i><i></i></span>正在查阅项目代码<span class="elapsed">{{ elapsed }}s</span></div>
          </div>
        </div>
      </div>

      <footer class="composer-wrap">
        <p v-if="error" class="error" role="alert">{{ error }}</p>
        <form class="composer" @submit.prevent="send()">
          <textarea
            ref="composer" v-model="input" rows="1" :disabled="!session" :placeholder="session ? `向 CodeChat 提问关于 ${projectName} 的问题` : '请先选择项目'"
            aria-label="输入问题" @keydown="onComposerKeydown" @input="resizeComposer"
          ></textarea>
          <button type="submit" class="send" :disabled="!session || busy || !input.trim()" aria-label="发送">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7" /></svg>
          </button>
        </form>
        <p class="hint">
          <span><kbd>Enter</kbd> 发送</span>
          <span><kbd v-for="key in newlineKeys" :key="key">{{ key }}</kbd> 换行</span>
        </p>
      </footer>
    </main>
  </div>
</template>

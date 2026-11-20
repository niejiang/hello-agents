<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref } from 'vue'
import { postEmpty, postJson, streamChat, type ChatEvent } from './api'
import { renderMarkdown } from './markdown'

type ToolCall = {
  id: string; name: string; arguments: unknown
  status: 'running' | 'ok' | 'error'; summary?: string; lines?: number; truncated?: boolean
}
type Part =
  | { kind: 'text'; text: string; html: string }
  | { kind: 'tools'; calls: ToolCall[] }
  | { kind: 'notice'; text: string }
type UserMessage = { role: 'user'; text: string }
type AssistantMessage = {
  role: 'assistant'; parts: Part[]; state: 'streaming' | 'done' | 'stopped' | 'error'
  answer?: string; error?: string; meta?: string
}
type Message = UserMessage | AssistantMessage

const SUGGESTIONS = ['这个项目的入口在哪里？', '帮我解释一下项目结构', '查找某个方法的实现并说明调用方']
// 各平台沿用主流网页 AI 对话的习惯：Enter 发送；Mac 用 Option+Enter、Windows 用 Shift+Enter 换行，两种组合在两个平台都可用。
const isMac = /mac/i.test((navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData?.platform ?? navigator.platform)
const newlineKeys = isMac ? ['⌥ Option', 'Enter'] : ['Shift', 'Enter']
// 停止请求发出后仍未结束（例如模型迟迟没有输出）时，直接断开连接。
const STOP_TIMEOUT_MS = 5000

const project = ref('')
const session = ref('')
const activeProject = ref('')
const editingProject = ref(true)
const sidebarOpen = ref(false)
const input = ref('')
const messages = ref<Message[]>([])
const busy = ref(false)
const stopping = ref(false)
const creating = ref(false)
const error = ref('')
const projectError = ref('')
const startedAt = ref(0)
const now = ref(0)
let ticker: number | undefined
let stopTimer: number | undefined
let controller: AbortController | null = null
let renderFrame = 0
let pinned = true

const conversation = ref<HTMLElement | null>(null)
const composer = ref<HTMLTextAreaElement | null>(null)
const projectInput = ref<HTMLInputElement | null>(null)

const projectName = computed(() => activeProject.value.split('/').filter(Boolean).pop() ?? '')
const elapsed = computed(() => Math.max(0, Math.floor((now.value - startedAt.value) / 1000)))
const status = computed(() => stopping.value ? '正在停止' : busy.value ? '回答中' : session.value ? '就绪' : '未选择项目')

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
    const data = await postJson('/api/sessions', { project_path: project.value })
    // 切换项目后释放旧会话，失败不影响新会话。
    if (session.value) fetch(`/api/sessions/${session.value}`, { method: 'DELETE' }).catch(() => {})
    session.value = data.session_id; activeProject.value = data.project_path; messages.value = []
    editingProject.value = false; sidebarOpen.value = false
    focusComposer()
  } catch (e) { projectError.value = e instanceof Error ? e.message : '无法创建会话' }
  finally { creating.value = false }
}

// 用户向上翻看时不强制滚动；回到底部附近后恢复跟随。
function onConversationScroll() {
  const el = conversation.value
  if (el) pinned = el.scrollHeight - el.scrollTop - el.clientHeight < 80
}

function scrollToBottom(force = false) {
  if (force) pinned = true
  if (!pinned) return
  nextTick(() => { const el = conversation.value; if (el) el.scrollTop = el.scrollHeight })
}

// 流式文本按帧合并渲染 Markdown，避免每个片段都重新解析。
function scheduleRender(reply: AssistantMessage) {
  if (renderFrame) return
  renderFrame = requestAnimationFrame(() => { renderFrame = 0; renderParts(reply) })
}

function renderParts(reply: AssistantMessage) {
  for (const part of reply.parts) if (part.kind === 'text') part.html = renderMarkdown(part.text)
  scrollToBottom()
}

function lastPart(reply: AssistantMessage) {
  return reply.parts[reply.parts.length - 1]
}

function findCall(reply: AssistantMessage, id: string) {
  for (const part of reply.parts) if (part.kind === 'tools') {
    const call = part.calls.find(item => item.id === id)
    if (call) return call
  }
}

function formatMeta(event: Extract<ChatEvent, { type: 'done' }>) {
  const items = []
  if (event.usage.last_prompt_tokens) items.push(`输入 ${event.usage.last_prompt_tokens.toLocaleString()} token`)
  items.push(`上下文 ${event.context.budget_percent}%`, `${event.usage.requests} 次请求`)
  if (event.context.trimmed_turns) items.push(`已移除 ${event.context.trimmed_turns} 轮早期对话`)
  return items.join(' · ')
}

class StreamError extends Error {}

function applyEvent(reply: AssistantMessage, event: ChatEvent) {
  const last = lastPart(reply)
  switch (event.type) {
    case 'text_delta':
      if (last?.kind === 'text') last.text += event.text
      else reply.parts.push({ kind: 'text', text: event.text, html: '' })
      scheduleRender(reply)
      break
    case 'tool_start': {
      const call: ToolCall = { id: event.id, name: event.name, arguments: event.arguments, status: 'running' }
      if (last?.kind === 'tools') last.calls.push(call)
      else reply.parts.push({ kind: 'tools', calls: [call] })
      scrollToBottom()
      break
    }
    case 'tool_end': {
      const call = findCall(reply, event.id)
      if (call) Object.assign(call, { status: event.ok ? 'ok' : 'error', summary: event.summary, lines: event.lines, truncated: event.truncated })
      break
    }
    case 'context_trimmed':
      reply.parts.push({ kind: 'notice', text: event.reason })
      break
    case 'done':
      reply.state = 'done'; reply.answer = event.answer; reply.meta = formatMeta(event)
      break
    case 'stopped':
      reply.state = 'stopped'
      break
    case 'error':
      throw new StreamError(event.message)
  }
}

async function send(preset?: string) {
  const text = (preset ?? input.value).trim()
  if (!text || busy.value) return
  if (!session.value) { projectError.value = '请先选择要分析的项目'; openProjectEditor(); return }
  busy.value = true; error.value = ''
  if (preset === undefined) input.value = ''
  nextTick(resizeComposer)
  messages.value.push({ role: 'user', text }, { role: 'assistant', parts: [], state: 'streaming' })
  // 通过响应式数组取回对象，后续修改才会触发界面更新。
  const reply = messages.value[messages.value.length - 1] as AssistantMessage
  startedAt.value = now.value = Date.now()
  ticker = window.setInterval(() => { now.value = Date.now() }, 1000)
  scrollToBottom(true)
  controller = new AbortController()
  try {
    for await (const event of streamChat(`/api/sessions/${session.value}/messages/stream`, { message: text }, controller.signal)) {
      applyEvent(reply, event)
    }
    if (reply.state === 'streaming') throw new StreamError('连接中断，回答不完整')
  } catch (e) {
    const message = e instanceof Error ? e.message : '发送失败，请重试'
    if (controller.signal.aborted) reply.state = 'stopped'
    else if (!reply.parts.length) {
      // 尚未收到任何内容：撤回这次提问，问题放回输入框便于重试。
      messages.value.splice(-2, 2)
      error.value = message
      if (!input.value) input.value = text
    } else { reply.state = 'error'; reply.error = message }
  } finally {
    cancelAnimationFrame(renderFrame); renderFrame = 0
    renderParts(reply)
    window.clearInterval(ticker); window.clearTimeout(stopTimer)
    controller = null
    busy.value = false; stopping.value = false
    focusComposer()
    nextTick(resizeComposer)
  }
}

async function stop() {
  if (!busy.value || stopping.value) return
  stopping.value = true
  const current = controller
  stopTimer = window.setTimeout(() => current?.abort(), STOP_TIMEOUT_MS)
  // 优先让后端正常结束本轮（收到 stopped 事件），失败时直接断开连接。
  try { await postEmpty(`/api/sessions/${session.value}/stop`) } catch { current?.abort() }
}

function isComposing(event: KeyboardEvent) {
  // keyCode 229 兼容 Safari 中输入法选词结束时的回车事件。
  return event.isComposing || event.keyCode === 229
}

function onComposerKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && busy.value) { event.preventDefault(); stop(); return }
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

function argsOf(call: ToolCall): Record<string, unknown> {
  return call.arguments && typeof call.arguments === 'object' ? call.arguments as Record<string, unknown> : {}
}

function describeCall(call: ToolCall) {
  if (typeof call.arguments === 'string') return `${call.name}（参数无效）`
  const args = argsOf(call)
  if (call.name === 'read_file') {
    const range = args.start || args.end ? `:${args.start ?? 1}-${args.end ?? ''}` : ''
    return `读取 ${args.path ?? '?'}${range}`
  }
  if (call.name === 'search_code') return `搜索 “${args.query ?? ''}”`
  if (call.name === 'list_files') return `列出 ${args.path || '项目根目录'}`
  return `调用 ${call.name}`
}

function callMeta(call: ToolCall) {
  if (call.status === 'running') return '进行中'
  if (call.status === 'error') return '失败'
  return `${call.lines ?? 0} 行${call.truncated ? '·已截断' : ''}`
}

function toolsTitle(calls: ToolCall[]) {
  const running = calls.find(call => call.status === 'running')
  if (running) return `正在${describeCall(running)}`
  const failed = calls.filter(call => call.status === 'error').length
  return `查阅了 ${calls.length} 处代码${failed ? `，${failed} 次失败` : ''}`
}

// 等待模型输出时显示的提示：还没有内容，或工具刚执行完、模型尚未继续输出。
function waitingLabel(reply: AssistantMessage) {
  if (reply.state !== 'streaming') return ''
  const last = lastPart(reply)
  if (!last) return '正在思考'
  if (last.kind === 'tools' && last.calls.every(call => call.status !== 'running')) return '正在整理结果'
  return ''
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

function copyMessage(event: MouseEvent, message: AssistantMessage) {
  const text = message.answer ?? message.parts.flatMap(part => part.kind === 'text' ? [part.text] : []).join('\n\n')
  copyText(event.currentTarget as HTMLElement, text)
}

onBeforeUnmount(() => { window.clearInterval(ticker); window.clearTimeout(stopTimer); controller?.abort() })
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

      <div ref="conversation" class="conversation" @click="onConversationClick" @scroll.passive="onConversationScroll">
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
              <div class="bubble">{{ message.text }}</div>
            </article>
            <article v-else :class="['msg', 'assistant', `state-${message.state}`]">
              <div class="avatar">C</div>
              <div class="msg-main">
                <template v-for="(part, i) in message.parts" :key="i">
                  <div v-if="part.kind === 'text' && part.html" class="markdown" v-html="part.html"></div>
                  <div v-else-if="part.kind === 'text'" class="markdown plain">{{ part.text }}</div>
                  <details v-else-if="part.kind === 'tools'" class="tools">
                    <summary>
                      <span :class="['tool-icon', part.calls.some(c => c.status === 'running') ? 'is-running' : 'is-ok']"></span>
                      <span class="tools-title">{{ toolsTitle(part.calls) }}</span>
                      <svg class="chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6" /></svg>
                    </summary>
                    <ul>
                      <li v-for="call in part.calls" :key="call.id" :class="`is-${call.status}`" :title="call.summary">
                        <span :class="['tool-icon', `is-${call.status}`]"></span>
                        <code>{{ describeCall(call) }}</code>
                        <span class="tool-meta">{{ callMeta(call) }}</span>
                        <small v-if="call.status === 'error' && call.summary" class="tool-error">{{ call.summary }}</small>
                      </li>
                    </ul>
                  </details>
                  <p v-else class="notice">{{ part.text }}</p>
                </template>
                <div v-if="waitingLabel(message)" class="typing-body">
                  <span class="dots"><i></i><i></i><i></i></span>{{ waitingLabel(message) }}<span class="elapsed">{{ elapsed }}s</span>
                </div>
                <p v-if="message.state === 'stopped'" class="state-note">已停止回答，本次内容不会计入对话历史。</p>
                <p v-if="message.state === 'error'" class="state-note error-note">{{ message.error }}</p>
                <div v-if="message.state !== 'streaming'" class="msg-actions">
                  <button v-if="message.parts.some(p => p.kind === 'text')" class="ghost-button small" @click="copyMessage($event, message)">复制</button>
                  <span v-if="message.meta" class="msg-meta">{{ message.meta }}</span>
                </div>
              </div>
            </article>
          </template>
        </div>
      </div>

      <footer class="composer-wrap">
        <p v-if="error" class="error" role="alert">{{ error }}</p>
        <form class="composer" @submit.prevent="send()">
          <textarea
            ref="composer" v-model="input" rows="1" :disabled="!session" :placeholder="session ? `向 CodeChat 提问关于 ${projectName} 的问题` : '请先选择项目'"
            aria-label="输入问题" @keydown="onComposerKeydown" @input="resizeComposer"
          ></textarea>
          <button v-if="busy" type="button" class="send stop" :disabled="stopping" aria-label="停止回答" title="停止回答（Esc）" @click="stop">
            <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="2" /></svg>
          </button>
          <button v-else type="submit" class="send" :disabled="!session || !input.trim()" aria-label="发送">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7" /></svg>
          </button>
        </form>
        <p class="hint">
          <span><kbd>Enter</kbd> 发送</span>
          <span><kbd v-for="key in newlineKeys" :key="key">{{ key }}</kbd> 换行</span>
          <span v-if="busy"><kbd>Esc</kbd> 停止</span>
        </p>
      </footer>
    </main>
  </div>
</template>

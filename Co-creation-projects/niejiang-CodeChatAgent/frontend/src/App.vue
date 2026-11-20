<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  HttpError, deleteEmpty, getJson, postEmpty, postJson, streamChat,
  type ChatEvent, type ProjectProfile, type SessionProject,
} from './api'
import { projectHue, renderMarkdown } from './markdown'

type ToolCall = {
  id: string; name: string; arguments: unknown
  status: 'running' | 'ok' | 'error'; summary?: string; lines?: number; truncated?: boolean
  // open_project 成功后由 project_opened 事件补充的项目信息。
  opened?: { name: string; stack: string[]; path: string }
}
type Part =
  | { kind: 'text'; text: string; html: string }
  | { kind: 'tools'; calls: ToolCall[] }
  | { kind: 'notice'; text: string }
  | { kind: 'project'; name: string; stack: string[]; path: string }
type UserMessage = { role: 'user'; text: string }
type AssistantMessage = {
  role: 'assistant'; parts: Part[]; state: 'streaming' | 'done' | 'stopped' | 'error'
  answer?: string; error?: string; meta?: string
}
type Message = UserMessage | AssistantMessage
// send 为 false 时只填入输入框，由用户补全（如项目路径）后再发送。
type Suggestion = { text: string; send: boolean }
// 各平台沿用主流网页 AI 对话的习惯：Enter 发送；Mac 用 Option+Enter、Windows 用 Shift+Enter 换行，两种组合在两个平台都可用。
const isMac = /mac/i.test((navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData?.platform ?? navigator.platform)
const newlineKeys = isMac ? ['⌥ Option', 'Enter'] : ['Shift', 'Enter']
// 停止请求发出后仍未结束（例如模型迟迟没有输出）时，直接断开连接。
const STOP_TIMEOUT_MS = 5000

// 会话在发送第一条消息时创建；项目在对话中按需打开。
const session = ref('')
const sessionProjects = ref<SessionProject[]>([])
const authorized = ref<ProjectProfile[]>([])
const confirmingRevoke = ref('')
const sidebarError = ref('')
const sidebarOpen = ref(false)
const input = ref('')
const messages = ref<Message[]>([])
const busy = ref(false)
const stopping = ref(false)
const error = ref('')
const startedAt = ref(0)
const now = ref(0)
let ticker: number | undefined
let stopTimer: number | undefined
let controller: AbortController | null = null
let renderFrame = 0
let pinned = true

const conversation = ref<HTMLElement | null>(null)
const composer = ref<HTMLTextAreaElement | null>(null)

const elapsed = computed(() => Math.max(0, Math.floor((now.value - startedAt.value) / 1000)))
const status = computed(() => stopping.value ? '正在停止' : busy.value ? '回答中' : '就绪')
const openPaths = computed(() => new Set(sessionProjects.value.map(project => project.path)))
const suggestions = computed<Suggestion[]>(() => {
  const recent = authorized.value[0]
  const general = recent
    ? [{ text: `${recent.name} 的项目结构是怎样的？`, send: true }, { text: `${recent.name} 的入口在哪里？`, send: true }]
    : [{ text: '用一个简单的例子解释什么是闭包', send: true }, { text: 'REST API 的分页应该怎么设计？', send: true }]
  return [...general, { text: '分析一个本地项目：~/', send: false }]
})
const hue = (name: string) => `hue-${projectHue(name)}`
const errorText = (e: unknown, fallback: string) => e instanceof Error ? e.message : fallback

function focusComposer() {
  nextTick(() => composer.value?.focus())
}

// 侧栏的项目信息获取失败不影响聊天。
async function refreshProjects() {
  try {
    const [all, current] = await Promise.all([
      getJson('/api/projects'),
      session.value ? getJson(`/api/sessions/${session.value}/projects`) : { projects: [] },
    ])
    authorized.value = all.projects
    sessionProjects.value = current.projects
  } catch { /* 忽略 */ }
}

async function ensureSession() {
  if (!session.value) session.value = (await postJson('/api/sessions')).session_id
  return session.value
}

function resetSession() {
  session.value = ''
  sessionProjects.value = []
}

function newChat() {
  if (busy.value) return
  // 释放旧会话，失败不影响新对话。
  if (session.value) deleteEmpty(`/api/sessions/${session.value}`).catch(() => {})
  resetSession()
  messages.value = []; error.value = ''; sidebarOpen.value = false
  focusComposer()
}

// 把已授权项目加入当前对话，不产生新的授权。
async function addToSession(profile: ProjectProfile) {
  if (busy.value || openPaths.value.has(profile.path)) return
  sidebarError.value = ''
  try {
    await ensureSession()
    sessionProjects.value = (await postJson(`/api/sessions/${session.value}/projects`, { path: profile.path })).projects
    sidebarOpen.value = false
    focusComposer()
  } catch (e) { sidebarError.value = errorText(e, '无法加入项目') }
}

// 第一次点击进入确认状态，再次点击才撤销授权。
async function revoke(profile: ProjectProfile) {
  if (confirmingRevoke.value !== profile.path) { confirmingRevoke.value = profile.path; return }
  confirmingRevoke.value = ''; sidebarError.value = ''
  try {
    await postEmpty('/api/projects/revoke', { path: profile.path })
    await refreshProjects()
  } catch (e) { sidebarError.value = errorText(e, '撤销授权失败') }
}

function useSuggestion(item: Suggestion) {
  if (item.send) { send(item.text); return }
  input.value = item.text
  nextTick(() => {
    resizeComposer()
    const el = composer.value
    if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length) }
  })
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
    case 'project_opened': {
      // 并入对应的 open_project 调用，避免把同一组工具卡片拆开；找不到时单独显示一行。
      const opened = { name: event.name, stack: event.stack, path: event.path }
      const call = reply.parts.flatMap(part => part.kind === 'tools' ? part.calls : [])
        .reverse().find(item => item.name === 'open_project' && !item.opened)
      if (call) call.opened = opened
      else reply.parts.push({ kind: 'project', ...opened })
      refreshProjects()
      break
    }
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
    await ensureSession()
    for await (const event of streamChat(`/api/sessions/${session.value}/messages/stream`, { message: text }, controller.signal)) {
      applyEvent(reply, event)
    }
    if (reply.state === 'streaming') throw new StreamError('连接中断，回答不完整')
  } catch (e) {
    let message = errorText(e, '发送失败，请重试')
    if (e instanceof HttpError && e.status === 404) {
      // 服务重启后旧会话已不存在：开始新对话，请用户重新发送。
      resetSession()
      message = '会话已失效（服务可能已重启），已开始新对话，请重新发送'
    }
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
    refreshProjects()
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

function resizeComposer() {
  const el = composer.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 220)}px`
}

function argsOf(call: ToolCall): Record<string, unknown> {
  return call.arguments && typeof call.arguments === 'object' ? call.arguments as Record<string, unknown> : {}
}

// 与回答中的引用格式一致：指定了项目时写成“项目:路径”。
function describeCall(call: ToolCall) {
  if (typeof call.arguments === 'string') return `${call.name}（参数无效）`
  const args = argsOf(call)
  const project = args.project ? `${args.project}:` : ''
  if (call.name === 'open_project') return `打开项目 ${call.opened?.name ?? args.path ?? ''}`
  if (call.name === 'read_file') {
    const range = args.start || args.end ? `:${args.start ?? 1}-${args.end ?? ''}` : ''
    return `读取 ${project}${args.path ?? '?'}${range}`
  }
  if (call.name === 'search_code') return `搜索 “${args.query ?? ''}”${args.project ? `（${args.project}）` : ''}`
  if (call.name === 'list_files') return `列出 ${project}${args.path && args.path !== '.' ? args.path : '根目录'}`
  return `调用 ${call.name}`
}

function callMeta(call: ToolCall) {
  if (call.status === 'running') return '进行中'
  if (call.status === 'error') return '失败'
  if (call.opened) return call.opened.stack.join(' · ') || '未识别技术栈'
  return `${call.lines ?? 0} 行${call.truncated ? '·已截断' : ''}`
}

function toolsTitle(calls: ToolCall[]) {
  const running = calls.find(call => call.status === 'running')
  if (running) return `正在${describeCall(running)}`
  const opened = calls.flatMap(call => call.opened ? [call.opened.name] : [])
  const lookups = calls.filter(call => call.name !== 'open_project').length
  const failed = calls.filter(call => call.status === 'error').length
  const items = [opened.length ? `打开项目 ${opened.join('、')}` : '', lookups ? `查阅 ${lookups} 次` : '']
  const title = items.filter(Boolean).join(' · ') || `执行了 ${calls.length} 次操作`
  return failed ? `${title}，${failed} 次失败` : title
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

onMounted(refreshProjects)
onBeforeUnmount(() => { window.clearInterval(ticker); window.clearTimeout(stopTimer); controller?.abort() })
</script>

<template>
  <div class="layout">
    <aside :class="['sidebar', { open: sidebarOpen }]">
      <div class="brand">
        <span class="brand-mark">C</span>
        <div class="brand-text"><strong>CodeChat</strong><small>对话与代码分析</small></div>
        <button class="icon-button only-mobile" aria-label="关闭侧栏" @click="sidebarOpen = false">✕</button>
      </div>

      <button class="new-chat" :disabled="busy" @click="newChat">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>新对话
      </button>

      <section class="panel">
        <div class="panel-title">当前对话的项目</div>
        <ul v-if="sessionProjects.length" class="project-list">
          <li v-for="item in sessionProjects" :key="item.path" class="project-row" :title="item.path">
            <span :class="['project-dot', hue(item.alias)]"></span>
            <div class="project-meta">
              <strong>{{ item.alias }}</strong>
              <small>{{ item.stack.join(' · ') || '未识别技术栈' }}</small>
            </div>
            <span v-if="item.recent && sessionProjects.length > 1" class="tag">最近</span>
          </li>
        </ul>
        <p v-else class="panel-note">尚未打开项目。在消息中写出项目路径即可，如 <code>~/Projects/my-app</code>。</p>
      </section>

      <section class="panel grow">
        <div class="panel-title">已授权项目<span v-if="authorized.length" class="count">{{ authorized.length }}</span></div>
        <ul v-if="authorized.length" class="project-list">
          <li v-for="item in authorized" :key="item.path" class="project-row action">
            <button
              class="project-open" :disabled="busy || openPaths.has(item.path)"
              :title="openPaths.has(item.path) ? '已在当前对话中打开' : `加入当前对话：${item.path}`" @click="addToSession(item)"
            >
              <span :class="['project-dot', hue(item.name)]"></span>
              <span class="project-meta"><strong>{{ item.name }}</strong><small>{{ item.path }}</small></span>
              <span v-if="openPaths.has(item.path)" class="tag">已打开</span>
            </button>
            <button
              :class="['revoke', { confirm: confirmingRevoke === item.path }]" :aria-label="`撤销 ${item.name} 的授权`"
              @click="revoke(item)" @blur="confirmingRevoke = ''"
            >{{ confirmingRevoke === item.path ? '撤销授权' : '✕' }}</button>
          </li>
        </ul>
        <p v-else class="panel-note">打开过的项目会出现在这里，之后直接说项目名称即可。</p>
        <p v-if="sidebarError" class="error" role="alert">{{ sidebarError }}</p>
      </section>

      <p class="sidebar-foot">已授权项目中被读取的代码片段会发送给配置的模型服务。对话仅保留在服务运行期间。</p>
    </aside>
    <div v-if="sidebarOpen" class="scrim only-mobile" @click="sidebarOpen = false"></div>

    <main class="main">
      <header class="topbar">
        <button class="icon-button only-mobile" aria-label="打开侧栏" @click="sidebarOpen = true">☰</button>
        <div class="topbar-title">
          <strong>{{ messages.length ? '对话' : '新对话' }}</strong>
          <span v-for="item in sessionProjects.slice(0, 3)" :key="item.path" :class="['chip', hue(item.alias)]" :title="item.path">{{ item.alias }}</span>
          <span v-if="sessionProjects.length > 3" class="chip more">+{{ sessionProjects.length - 3 }}</span>
        </div>
        <span :class="['status', { ready: !busy, busy }]"><i></i>{{ status }}</span>
      </header>

      <div ref="conversation" class="conversation" @click="onConversationClick" @scroll.passive="onConversationScroll">
        <div class="thread">
          <section v-if="!messages.length" class="welcome">
            <div class="welcome-mark">✳</div>
            <h1>有什么可以帮你？</h1>
            <p>可以直接聊天；需要分析代码时，在消息里写出项目路径，或提到已授权项目的名称。</p>
            <div class="suggestions">
              <button v-for="item in suggestions" :key="item.text" class="suggestion" :disabled="busy" @click="useSuggestion(item)">
                <span>{{ item.text }}</span><span class="arrow">{{ item.send ? '→' : '✎' }}</span>
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
                        <code :title="call.opened?.path">{{ describeCall(call) }}</code>
                        <span class="tool-meta">{{ callMeta(call) }}</span>
                        <small v-if="call.status === 'error' && call.summary" class="tool-error">{{ call.summary }}</small>
                      </li>
                    </ul>
                  </details>
                  <p v-else-if="part.kind === 'project'" class="opened" :title="part.path">
                    <span :class="['project-dot', hue(part.name)]"></span>已打开项目 <b>{{ part.name }}</b>
                    <span class="opened-stack">{{ part.stack.join(' · ') || '未识别技术栈' }}</span>
                  </p>
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
            ref="composer" v-model="input" rows="1" placeholder="给 CodeChat 发消息；分析代码时写出项目路径"
            aria-label="输入问题" @keydown="onComposerKeydown" @input="resizeComposer"
          ></textarea>
          <button v-if="busy" type="button" class="send stop" :disabled="stopping" aria-label="停止回答" title="停止回答（Esc）" @click="stop">
            <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="2" /></svg>
          </button>
          <button v-else type="submit" class="send" :disabled="!input.trim()" aria-label="发送">
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

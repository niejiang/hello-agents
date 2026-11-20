<script setup lang="ts">
import { ref, nextTick } from 'vue'

type ContentBlock = { type: 'text'; text: string } | { type: string; text?: string }
type Message = { role: 'user' | 'assistant'; content: ContentBlock[] }
const project = ref('')
const session = ref('')
const activeProject = ref('')
const input = ref('')
const messages = ref<Message[]>([])
const busy = ref(false)
const error = ref('')
const conversation = ref<HTMLElement | null>(null)

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
async function createSession() {
  if (!project.value.trim() || busy.value) return
  busy.value = true; error.value = ''
  try {
    const data = await request('/api/sessions', { project_path: project.value })
    // 切换项目后释放旧会话，失败不影响新会话。
    if (session.value) fetch(`/api/sessions/${session.value}`, { method: 'DELETE' }).catch(() => {})
    session.value = data.session_id; activeProject.value = data.project_path; messages.value = []
  } catch (e) { error.value = e instanceof Error ? e.message : '无法创建会话' }
  finally { busy.value = false }
}
async function send() {
  const text = input.value.trim()
  if (!text || !session.value || busy.value) return
  busy.value = true; error.value = ''; input.value = ''
  messages.value.push({ role: 'user', content: [{ type: 'text', text }] })
  try {
    const data = await request(`/api/sessions/${session.value}/messages`, { message: text })
    messages.value.push({ role: 'assistant', content: data.content })
  } catch (e) {
    messages.value.pop()
    error.value = e instanceof Error ? e.message : '发送失败，请重试'
    input.value = text
  }
  finally {
    busy.value = false
    await nextTick()
    conversation.value?.scrollTo({ top: conversation.value.scrollHeight, behavior: 'smooth' })
  }
}
// 输入法选词时的回车不发送；keyCode 229 兼容 Safari 选词结束时的回车事件。
function onComposerEnter(event: KeyboardEvent) {
  if (event.isComposing || event.keyCode === 229) return
  event.preventDefault()
  send()
}
function onProjectEnter(event: KeyboardEvent) {
  if (event.isComposing || event.keyCode === 229) return
  createSession()
}
</script>

<template>
  <div class="layout">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark">C</span><div><strong>CodeChat</strong><small>代码分析助手</small></div></div>
      <div class="sidebar-section"><span class="eyebrow">WORKSPACE</span><h2>选择项目</h2><p>输入本机项目的绝对路径，开始一段新的对话。</p>
        <label for="project">项目路径</label>
        <input id="project" v-model="project" placeholder="/Users/你的名字/项目" :disabled="busy" @keydown.enter="onProjectEnter">
        <button class="project-button" :disabled="busy || !project.trim()" @click="createSession">{{ session ? '切换项目并新建对话' : '开始对话' }}</button>
      </div>
      <div v-if="activeProject" class="current"><span class="status-dot"></span><div><small>当前项目</small><p>{{ activeProject }}</p></div></div>
      <div class="sidebar-foot">代码片段可能发送至模型服务。<br>请选择允许分析的项目。</div>
    </aside>
    <main class="main">
      <header class="topbar"><div><span class="eyebrow">YOUR WORKSPACE</span><h1>代码对话</h1></div><span class="connection">{{ session ? '会话已就绪' : '等待选择项目' }}</span></header>
      <div ref="conversation" class="conversation" aria-live="polite">
        <div v-if="!messages.length" class="welcome"><div class="welcome-icon">✳</div><span class="eyebrow">CODECHAT AGENT</span><h2>从一个问题开始</h2><p>选择本地项目后，聊聊你想了解的代码。助手会结合项目内容为你解答。</p><div class="suggestions"><span>这个项目的入口在哪里？</span><span>帮我解释项目结构</span><span>查找某个方法的实现</span></div></div>
        <div v-else class="messages"><article v-for="(message, index) in messages" :key="index" :class="['message', message.role]"><div class="avatar">{{ message.role === 'user' ? '你' : 'C' }}</div><div class="message-body"><strong>{{ message.role === 'user' ? '你' : 'CodeChat' }}</strong><template v-for="(block, i) in message.content" :key="i"><div v-if="block.type === 'text'" class="message-text">{{ block.text }}</div><div v-else class="unsupported">暂不支持展示此内容类型。</div></template></div></article><div v-if="busy" class="thinking">助手正在思考，请稍候…</div></div>
      </div>
      <div class="composer-wrap"><p v-if="error" class="error" role="alert">{{ error }}</p><form class="composer" @submit.prevent="send"><textarea v-model="input" :disabled="!session || busy" placeholder="输入问题，Enter 发送，Shift + Enter 换行" rows="2" @keydown.enter.exact="onComposerEnter"></textarea><button type="submit" :disabled="!session || busy || !input.trim()">发送</button></form><p class="hint">{{ session ? '当前会话仅保留在服务运行期间。' : '请先选择项目并开始对话。' }}</p></div>
    </main>
  </div>
</template>

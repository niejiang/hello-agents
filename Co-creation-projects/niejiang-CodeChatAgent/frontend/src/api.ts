/** 后端接口调用：普通 JSON 请求与 SSE 流式回答。 */

export type ChatEvent =
  | { type: 'start' }
  | { type: 'text_delta'; text: string }
  | { type: 'tool_start'; id: string; name: string; arguments: unknown }
  | { type: 'tool_end'; id: string; name: string; ok: boolean; summary: string; lines: number; truncated: boolean }
  | { type: 'context_trimmed'; reason: string; scope?: 'history' | 'tool_results'; count?: number }
  | { type: 'stopped' }
  | { type: 'error'; code: string; message: string }
  | { type: 'done'; answer: string; usage: Usage; context: ContextInfo }

export type Usage = { requests: number; prompt_tokens: number; completion_tokens: number; cached_tokens: number; last_prompt_tokens: number }
export type ContextInfo = { budget: number; budget_percent: number; history_turns: number; trimmed_turns: number; cleared_tool_results?: number }

async function post(url: string, body?: object, signal?: AbortSignal) {
  let response: Response
  try {
    response = await fetch(url, {
      method: 'POST', signal,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    })
  } catch (e) {
    if (signal?.aborted) throw e
    throw new Error('无法连接后端服务，请确认服务已启动')
  }
  if (!response.ok) {
    // 后端未启动时代理可能返回非 JSON 内容，此时给出可读提示。
    const data = await response.json().catch(() => null)
    if (typeof data?.detail === 'string') throw new Error(data.detail)
    throw new Error(response.status >= 500 ? '后端服务不可用，请确认服务已启动' : '请求无效，请检查输入')
  }
  return response
}

export async function postJson(url: string, body: object) {
  const data = await (await post(url, body)).json().catch(() => null)
  if (data === null) throw new Error('服务返回了无法识别的内容')
  return data
}

export async function postEmpty(url: string) {
  await post(url)
}

/** 发送问题并逐条产出 SSE 事件；signal 中止时抛出 AbortError。 */
export async function* streamChat(url: string, body: object, signal: AbortSignal): AsyncGenerator<ChatEvent> {
  const response = await post(url, body, signal)
  if (!response.body) throw new Error('当前浏览器不支持流式响应')
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''
  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += value.replace(/\r\n/g, '\n')
      let end: number
      while ((end = buffer.indexOf('\n\n')) >= 0) {
        const block = buffer.slice(0, end)
        buffer = buffer.slice(end + 2)
        const data = block.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
        if (data) yield JSON.parse(data) as ChatEvent
      }
    }
  } finally {
    // 调用方提前结束迭代时取消读取，释放连接。
    reader.cancel().catch(() => {})
  }
}

/**
 * 把回答中的 ```mermaid 代码块渲染为图。
 *
 * - Mermaid 体积较大，首次遇到流程图时才动态加载。
 * - 回答结束（或打开历史对话）后再渲染；流式输出过程中源码不完整，先显示源码。
 * - 使用 strict 安全级别：图中不执行脚本、不允许点击跳转，标签中的 HTML 会被清理。
 * - 语法错误或加载失败时保留源码并给出提示，不影响其他内容。
 */

type Mermaid = typeof import('mermaid')['default']

let loader: Promise<Mermaid> | null = null
let counter = 0

function load(): Promise<Mermaid> {
  loader ??= import('mermaid').then(({ default: mermaid }) => {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      theme: 'neutral',
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
    })
    return mermaid
  }).catch(error => { loader = null; throw error })
  return loader
}

/** 模型有时把 < > 写成 HTML 转义，改为 Mermaid 的实体写法，节点文字才能正常显示。 */
function normalize(source: string): string {
  return source
    .replace(/&lt;/g, '#lt;')
    .replace(/&gt;/g, '#gt;')
    .replace(/&quot;/g, '#quot;')
    .replace(/&amp;/g, '#amp;')
}

function fail(block: HTMLElement, message: string) {
  block.dataset.diagram = 'error'
  const note = block.querySelector<HTMLElement>('.diagram-error')
  if (note) { note.textContent = message; note.hidden = false }
}

/** 渲染 root 中所有待渲染的流程图。 */
export async function renderDiagrams(root: ParentNode | null): Promise<void> {
  if (!root) return
  const blocks = Array.from(root.querySelectorAll<HTMLElement>('.diagram-block[data-diagram="pending"]'))
  if (!blocks.length) return
  let mermaid: Mermaid
  try {
    mermaid = await load()
  } catch {
    blocks.forEach(block => fail(block, '流程图组件加载失败，已显示源码'))
    return
  }
  for (const block of blocks) {
    if (block.dataset.diagram !== 'pending' || !block.isConnected) continue
    block.dataset.diagram = 'rendering'
    const source = normalize(block.querySelector('.diagram-source code')?.textContent ?? '')
    const id = `codechat-diagram-${++counter}`
    try {
      if (!(await mermaid.parse(source, { suppressErrors: true }))) throw new Error('syntax')
      const { svg } = await mermaid.render(id, source)
      if (!block.isConnected) continue
      const view = block.querySelector<HTMLElement>('.diagram-view')!
      view.innerHTML = svg
      block.dataset.diagram = 'done'
      // 图被缩小到原始宽度的 70% 以下时提示可以放大查看。
      const width = view.querySelector('svg')?.viewBox.baseVal?.width ?? 0
      block.classList.toggle('is-shrunk', width > 0 && view.clientWidth / width < 0.7)
    } catch {
      // 渲染失败时 Mermaid 可能在页面上留下临时元素，一并清理。
      document.getElementById(id)?.remove()
      document.getElementById(`d${id}`)?.remove()
      fail(block, '流程图语法有误，已显示源码')
    }
  }
}

/** 在图和源码之间切换。 */
export function toggleDiagram(button: HTMLElement) {
  const block = button.closest<HTMLElement>('.diagram-block')
  if (!block || block.dataset.diagram !== 'done') return
  const showSource = block.classList.toggle('show-source')
  button.textContent = showSource ? '查看图' : '查看源码'
}

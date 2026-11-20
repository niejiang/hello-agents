/** 将模型回复渲染为安全的 HTML：禁用原始 HTML，代码块高亮，识别“路径:行号”引用。 */
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js/lib/core'
import bash from 'highlight.js/lib/languages/bash'
import css from 'highlight.js/lib/languages/css'
import go from 'highlight.js/lib/languages/go'
import java from 'highlight.js/lib/languages/java'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import python from 'highlight.js/lib/languages/python'
import sql from 'highlight.js/lib/languages/sql'
import typescript from 'highlight.js/lib/languages/typescript'
import xml from 'highlight.js/lib/languages/xml'
import yaml from 'highlight.js/lib/languages/yaml'
import 'highlight.js/styles/github-dark.css'

// 只注册常见语言，控制打包体积；未知语言按纯文本显示。
for (const [name, language] of Object.entries({ bash, css, go, java, javascript, json, python, sql, typescript, xml, yaml })) {
  hljs.registerLanguage(name, language)
}
hljs.registerAliases(['sh', 'shell', 'zsh'], { languageName: 'bash' })
hljs.registerAliases(['js', 'jsx'], { languageName: 'javascript' })
hljs.registerAliases(['ts', 'tsx'], { languageName: 'typescript' })
hljs.registerAliases(['py'], { languageName: 'python' })
hljs.registerAliases(['html', 'vue'], { languageName: 'xml' })
hljs.registerAliases(['yml'], { languageName: 'yaml' })

// html: false 会转义模型输出中的 HTML，v-html 渲染时不会执行其中的标签或脚本。
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
// 关闭模糊链接，否则 main.py 这类文件名会被当作 .py 域名转成链接。
md.linkify.set({ fuzzyLink: false })
const escape = md.utils.escapeHtml

md.renderer.rules.fence = (tokens, idx) => {
  const token = tokens[idx]
  const lang = token.info.trim().split(/\s+/)[0]?.toLowerCase() ?? ''
  const code = lang && hljs.getLanguage(lang)
    ? hljs.highlight(token.content, { language: lang, ignoreIllegals: true }).value
    : escape(token.content)
  return `<div class="code-block"><div class="code-head"><span>${escape(lang || 'text')}</span>`
    + `<button type="button" class="copy-code">复制</button></div><pre><code class="hljs">${code}</code></pre></div>`
}

const renderLink = md.renderer.rules.link_open
  ?? ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options))
md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  tokens[idx].attrSet('target', '_blank')
  tokens[idx].attrSet('rel', 'noopener noreferrer')
  return renderLink(tokens, idx, options, env, self)
}

// 形如 backend/main.py:19、app.py:10-20 的文件引用；打开多个项目时前面带项目简称，如 my-agent:src/tools.ts:42。
// 项目简称可能带上级目录（如 old/alpha），不含点号，以免把路径误当作简称。
const CITE = /(?:[\w-]+(?:\/[\w-]+)?:)?(?:[\w.-]+\/)*[\w.-]+\.[A-Za-z][A-Za-z0-9]*:\d+(?:-\d+)?/g
const isCite = (text: string) => new RegExp(`^${CITE.source}$`).test(text)
const CITE_PROJECT = /^([\w-]+(?:\/[\w-]+)?):(?=.*\.[A-Za-z][A-Za-z0-9]*:\d)/

/** 同一项目始终使用同一种颜色。 */
export function projectHue(name: string): number {
  let hash = 0
  for (const char of name) hash = (hash * 31 + char.codePointAt(0)!) >>> 0
  return hash % 6
}

function renderCite(text: string) {
  const match = CITE_PROJECT.exec(text)
  if (!match) return `<span class="cite">${escape(text)}</span>`
  const project = match[1]
  return `<span class="cite"><b class="cite-project hue-${projectHue(project)}">${escape(project)}:</b>`
    + `${escape(text.slice(match[0].length))}</span>`
}

md.renderer.rules.code_inline = (tokens, idx) => {
  const content = tokens[idx].content
  return isCite(content) ? renderCite(content) : `<code>${escape(content)}</code>`
}
md.renderer.rules.file_cite = (tokens, idx) => renderCite(tokens[idx].content)

// 将普通文本中的文件引用拆成独立标记，链接文字内部不处理。
md.core.ruler.push('file_cite', (state) => {
  for (const block of state.tokens) {
    if (block.type !== 'inline' || !block.children) continue
    const textToken = (content: string) => {
      const token = new state.Token('text', '', 0)
      token.content = content
      return token
    }
    const children: typeof block.children = []
    let linkDepth = 0
    for (const child of block.children) {
      if (child.type === 'link_open') linkDepth++
      if (child.type === 'link_close') linkDepth--
      if (child.type !== 'text' || linkDepth > 0) { children.push(child); continue }
      let last = 0
      for (const match of child.content.matchAll(CITE)) {
        const start = match.index ?? 0
        if (start > last) children.push(textToken(child.content.slice(last, start)))
        const cite = new state.Token('file_cite', '', 0)
        cite.content = match[0]
        children.push(cite)
        last = start + match[0].length
      }
      if (last === 0) children.push(child)
      else if (last < child.content.length) children.push(textToken(child.content.slice(last)))
    }
    block.children = children
  }
})

export function renderMarkdown(text: string): string {
  return md.render(text)
}

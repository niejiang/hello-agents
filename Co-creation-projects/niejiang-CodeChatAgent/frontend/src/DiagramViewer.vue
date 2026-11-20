<script setup lang="ts">
/**
 * 流程图全屏查看：滚轮或双指缩放（以光标/手指为中心）、拖动平移、适应窗口与原始大小。
 * 显示的是已渲染的 Mermaid SVG（strict 模式生成），不重新执行渲染。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

const props = defineProps<{ svg: string }>()
const emit = defineEmits<{ close: [] }>()

const MIN_SCALE = 0.1
const MAX_SCALE = 8
const STEP = 1.25

const stage = ref<HTMLElement | null>(null)
const canvas = ref<HTMLElement | null>(null)
const closeButton = ref<HTMLButtonElement | null>(null)
const scale = ref(1)
const x = ref(0)
const y = ref(0)
let natural = { width: 1, height: 1 }
const pointers = new Map<number, { x: number; y: number }>()
let lastPinch = 0
const previousFocus = document.activeElement as HTMLElement | null

const percent = computed(() => `${Math.round(scale.value * 100)}%`)
const transform = computed(() => `translate(${x.value}px, ${y.value}px) scale(${scale.value})`)
const clamp = (value: number) => Math.min(MAX_SCALE, Math.max(MIN_SCALE, value))

function stageSize() {
  const rect = stage.value?.getBoundingClientRect()
  return { width: rect?.width ?? 1, height: rect?.height ?? 1, left: rect?.left ?? 0, top: rect?.top ?? 0 }
}

/** 按 viewBox 设定 SVG 的原始尺寸，去掉内联的 max-width 限制，缩放由外层 transform 控制。 */
function prepare() {
  const svg = canvas.value?.querySelector('svg')
  if (!svg) return
  const box = svg.viewBox.baseVal
  const bounds = box && box.width ? box : svg.getBBox()
  natural = { width: bounds.width || 1, height: bounds.height || 1 }
  svg.removeAttribute('style')
  svg.setAttribute('width', String(natural.width))
  svg.setAttribute('height', String(natural.height))
  fit()
}

function center(nextScale: number) {
  const { width, height } = stageSize()
  scale.value = clamp(nextScale)
  x.value = (width - natural.width * scale.value) / 2
  y.value = (height - natural.height * scale.value) / 2
}

function fit() {
  const { width, height } = stageSize()
  center(Math.min(width / natural.width, height / natural.height) * 0.92)
}

function actualSize() {
  center(1)
}

/** 以舞台内的 (cx, cy) 为中心缩放，使该点在缩放前后位置不变。 */
function zoomAt(factor: number, cx?: number, cy?: number) {
  const { width, height } = stageSize()
  const px = cx ?? width / 2
  const py = cy ?? height / 2
  const next = clamp(scale.value * factor)
  const ratio = next / scale.value
  x.value = px - (px - x.value) * ratio
  y.value = py - (py - y.value) * ratio
  scale.value = next
}

function onWheel(event: WheelEvent) {
  event.preventDefault()
  const { left, top } = stageSize()
  // 触控板双指缩放会以 ctrl+滚轮的形式到达，幅度较小，放大系数以便手感一致。
  const speed = event.ctrlKey ? 0.01 : 0.0015
  zoomAt(Math.exp(-event.deltaY * speed), event.clientX - left, event.clientY - top)
}

function onPointerDown(event: PointerEvent) {
  event.preventDefault() // 避免拖动时选中图中文字
  stage.value?.setPointerCapture(event.pointerId)
  pointers.set(event.pointerId, { x: event.clientX, y: event.clientY })
  lastPinch = 0
}

function onPointerMove(event: PointerEvent) {
  const previous = pointers.get(event.pointerId)
  if (!previous) return
  const current = { x: event.clientX, y: event.clientY }
  pointers.set(event.pointerId, current)
  if (pointers.size === 1) {
    x.value += current.x - previous.x
    y.value += current.y - previous.y
    return
  }
  // 双指：按两指距离的变化缩放，以两指中点为中心。
  const [a, b] = [...pointers.values()]
  const distance = Math.hypot(a.x - b.x, a.y - b.y)
  if (lastPinch) {
    const { left, top } = stageSize()
    zoomAt(distance / lastPinch, (a.x + b.x) / 2 - left, (a.y + b.y) / 2 - top)
  }
  lastPinch = distance
}

function onPointerUp(event: PointerEvent) {
  pointers.delete(event.pointerId)
  lastPinch = 0
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); emit('close') }
  else if (event.key === '+' || event.key === '=') zoomAt(STEP)
  else if (event.key === '-') zoomAt(1 / STEP)
  else if (event.key === '0') fit()
}

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  window.addEventListener('resize', fit)
  nextTick(() => { prepare(); closeButton.value?.focus() })
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('resize', fit)
  previousFocus?.focus?.()
})
</script>

<template>
  <div class="viewer" role="dialog" aria-modal="true" aria-label="流程图查看">
    <header class="viewer-bar">
      <strong>流程图</strong>
      <div class="viewer-tools">
        <button type="button" title="缩小（-）" aria-label="缩小" @click="zoomAt(1 / STEP)">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14" /></svg>
        </button>
        <span class="viewer-percent">{{ percent }}</span>
        <button type="button" title="放大（+）" aria-label="放大" @click="zoomAt(STEP)">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
        </button>
        <button type="button" class="text" title="适应窗口（0）" @click="fit">适应窗口</button>
        <button type="button" class="text actual" @click="actualSize">原始大小</button>
        <button ref="closeButton" type="button" class="text close" title="关闭（Esc）" @click="emit('close')">关闭</button>
      </div>
    </header>
    <div
      ref="stage" class="viewer-stage"
      @wheel="onWheel" @pointerdown="onPointerDown" @pointermove="onPointerMove"
      @pointerup="onPointerUp" @pointercancel="onPointerUp" @dblclick="fit"
    >
      <!-- eslint-disable-next-line vue/no-v-html -->
      <div ref="canvas" class="viewer-canvas" :style="{ transform }" v-html="props.svg"></div>
    </div>
    <p class="viewer-hint">滚轮或双指缩放 · 拖动平移 · 双击适应窗口 · Esc 关闭</p>
  </div>
</template>

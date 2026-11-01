<script setup>
import { ref, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { forceSimulation, forceLink, forceManyBody, forceCenter, forceCollide, forceX, forceY } from 'd3-force'
import { select } from 'd3-selection'
import { drag } from 'd3-drag'
import { zoom, zoomIdentity } from 'd3-zoom'

const props = defineProps({
  nodes: { type: Array, default: () => [] },
  links: { type: Array, default: () => [] },
  active: { type: Object, default: null },
})
const emit = defineEmits(['select'])

const host = ref(null)
const svgRef = ref(null)
let sim = null
let simNodes = []
let simLinks = []

const PALETTE = ['#6FE3C4', '#7FB2D9', '#C9A6E0', '#F2B45A', '#9BD07A', '#E08FA8', '#8FD8D2', '#B9C46A']
function color(name) {
  let h = 0
  for (const ch of name || '') h = (h * 31 + ch.charCodeAt(0)) % 9973
  return PALETTE[h % PALETTE.length]
}
// 半径按名下卡数开方：面积与数量成正比，视觉上才不会把 158 张和 5 张画成一个量级
function radius(n) {
  return 12 + Math.sqrt(Math.max(n.cards || 1, 1)) * 3.4
}

function build() {
  if (!svgRef.value || !host.value) return
  const W = host.value.clientWidth
  const H = host.value.clientHeight
  const svg = select(svgRef.value)
  svg.selectAll('*').remove()
  svg.attr('viewBox', `0 0 ${W} ${H}`)

  // 深水背景：几层极淡的同心弧，暗示水层而不是装饰
  const defs = svg.append('defs')
  const grad = defs.append('linearGradient').attr('id', 'edgeGrad').attr('gradientUnits', 'userSpaceOnUse')
  grad.append('stop').attr('offset', '0%').attr('stop-color', '#6FE3C4').attr('stop-opacity', 0.45)
  grad.append('stop').attr('offset', '100%').attr('stop-color', '#7FB2D9').attr('stop-opacity', 0.12)

  const root = svg.append('g')

  simNodes = props.nodes.map((n) => ({ ...n, r: radius(n) }))
  const idOf = new Map(props.nodes.map((n, i) => [n.id, i]))
  simLinks = props.links
    .filter((l) => idOf.has(l.source) && idOf.has(l.target))
    .map((l) => ({ ...l, source: idOf.get(l.source), target: idOf.get(l.target) }))

  sim = forceSimulation(simNodes)
    .force('link', forceLink(simLinks).distance(96).strength(0.32))
    .force('charge', forceManyBody().strength((d) => -70 - d.r * 3))
    .force('center', forceCenter(W / 2, H / 2))
    .force('x', forceX(W / 2).strength(0.12))
    .force('y', forceY(H / 2).strength(0.16))
    .force('collide', forceCollide().radius((d) => d.r + 12))

  const link = root.append('g')
    .selectAll('line')
    .data(simLinks)
    .join('line')
    .attr('stroke', 'url(#edgeGrad)')
    .attr('stroke-width', (d) => Math.min(1 + d.weight * 0.35, 3.5))

  const node = root.append('g')
    .selectAll('g')
    .data(simNodes)
    .join('g')
    .attr('cursor', 'pointer')
    .on('click', (e, d) => emit('select', props.nodes.find((n) => n.id === d.id)))

  // 透明大招点：图上的点普遍只有十几像素，直接点它命中率很差
  // （Obsidian 图谱最常被吐槽的就是这个）。命中区放大到 2.4 倍，视觉不变。
  node.append('circle')
    .attr('class', 'hit')
    .attr('r', (d) => d.r + 14)
    .attr('fill', 'transparent')

  // 光晕：只在悬停/选中时亮，平时保持克制
  node.append('circle')
    .attr('class', 'halo')
    .attr('r', (d) => d.r + 9)
    .attr('fill', (d) => color(d.name))
    .attr('opacity', 0)

  node.append('circle')
    .attr('r', (d) => d.r)
    .attr('fill', (d) => color(d.name))
    .attr('fill-opacity', 0.9)
    .attr('stroke', '#07100e')
    .attr('stroke-width', 1.5)

  node.append('text')
    .attr('text-anchor', 'middle')
    .attr('dy', '0.36em')
    .attr('font-family', 'ui-monospace, monospace')
    .attr('font-size', (d) => Math.max(9, Math.min(14, d.r * 0.62)))
    .attr('fill', '#06211c')
    .attr('font-weight', '700')
    .text((d) => (d.name || '?').trim().slice(0, 1).toUpperCase())

  // 只给大点打标签，小点靠悬停看名字——否则 37 个标签会糊成一片
  node.append('text')
    .attr('class', 'label')
    .attr('text-anchor', 'middle')
    .attr('dy', (d) => d.r + 14)
    .attr('font-size', 10)
    .attr('fill', '#8FA5A2')
    .attr('opacity', (d) => (d.r >= 17 ? 1 : 0))
    .text((d) => ((d.name || '').length > 11 ? d.name.slice(0, 11) + '…' : d.name))

  node.append('title').text((d) => `${d.name} · ${d.cards} 张卡`)

  node
    .on('mouseenter', function () { select(this).select('.halo').transition().duration(180).attr('opacity', 0.22) })
    .on('mouseleave', function () { select(this).select('.halo').transition().duration(220).attr('opacity', 0.12) })

  node.call(drag()
    .on('start', (e, d) => { if (!e.active) sim.alphaTarget(0.25).restart(); d.fx = d.x; d.fy = d.y })
    .on('drag', (e, d) => { d.fx = e.x; d.fy = e.y })
    .on('end', (e, d) => { if (!e.active) sim.alphaTarget(0); d.fx = null; d.fy = null }))

  // 边界夹取：不做的话大节点会被斥力推到画布外，视觉上像被裁掉了
  sim.on('tick', () => {
    for (const d of simNodes) {
      d.x = Math.max(d.r + 6, Math.min(W - d.r - 6, d.x))
      d.y = Math.max(d.r + 6, Math.min(H - d.r - 22, d.y))
    }
    link
      .attr('x1', (d) => d.source.x).attr('y1', (d) => d.source.y)
      .attr('x2', (d) => d.target.x).attr('y2', (d) => d.target.y)
    node.attr('transform', (d) => `translate(${d.x},${d.y})`)
  })

  svg.call(zoom().scaleExtent([0.4, 2.4]).on('zoom', (e) => root.attr('transform', e.transform)))

  // 入场：整张图从透明淡入，节点依次点亮
  node.attr('opacity', 0)
  node.transition().delay((d, i) => i * 45).duration(520).attr('opacity', 1)
  link.attr('stroke-opacity', 0).transition().duration(900).attr('stroke-opacity', 1)
}

function highlight() {
  if (!svgRef.value) return
  const svg = select(svgRef.value)
  const id = props.active?.id
  svg.selectAll('circle.halo').classed('pulse', false).attr('opacity', 0.10)
  if (id == null) return
  const hit = simNodes.find((n) => n.id === id)
  if (!hit) return
  // 选中态用呼吸动画而不是常亮：Neural Vault 那个"像大脑"的感觉来自时序，不是亮度
  svg.selectAll('g').filter((d) => d && d.id === id).select('.halo').classed('pulse', true)
}

watch(() => [props.nodes, props.links], () => nextTick(build))
watch(() => props.active, () => highlight())

let ro = null
onMounted(() => {
  build()
  ro = new ResizeObserver(() => { if (sim) { sim.stop(); build() } })
  ro.observe(host.value)
})
onBeforeUnmount(() => { if (sim) sim.stop(); if (ro) ro.disconnect() })
</script>

<template>
  <div class="graph-wrap">
    <div class="graph" ref="host">
      <svg ref="svgRef"></svg>
    </div>
    <div class="graph-legend">
      点的大小 = 名下卡片数 · 连线 = 在同一段记忆里一起出现 · 可拖动、可缩放、点一下看详情
    </div>
  </div>
</template>

<style scoped>
.graph {
  position: relative;
  height: 460px;
  border: 1px solid var(--line);
  border-radius: 16px;
  overflow: hidden;
  background:
    radial-gradient(680px 340px at 50% -20%, rgba(111, 227, 196, 0.10), transparent 62%),
    radial-gradient(520px 300px at 84% 110%, rgba(127, 178, 217, 0.10), transparent 66%),
    linear-gradient(180deg, #0b1418, #070c0f);
}
.graph svg { width: 100%; height: 100%; display: block; }
:deep(.halo.pulse) { animation: breathe 2.6s ease-in-out infinite; }
@keyframes breathe {
  0%, 100% { opacity: 0.28; }
  50% { opacity: 0.62; }
}
.graph-wrap { display: block; }
.graph-legend {
  margin-top: 10px;
  font-family: var(--mono); font-size: 10px; letter-spacing: 0.05em;
  color: var(--ink-3);
}
</style>

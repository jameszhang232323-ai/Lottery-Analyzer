<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { init, use, type ECharts, type EChartsCoreOption } from 'echarts/core'
import { BarChart } from 'echarts/charts'
import { GridComponent, TitleComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

use([BarChart, GridComponent, TitleComponent, TooltipComponent, CanvasRenderer])

const props = withDefaults(
  defineProps<{
    option: EChartsCoreOption
    height?: number
    label: string
  }>(),
  { height: 260 },
)

const element = ref<HTMLDivElement | null>(null)
let chart: ECharts | null = null
let observer: ResizeObserver | null = null

onMounted(() => {
  if (!element.value) return
  chart = init(element.value)
  chart.setOption(props.option)
  observer = new ResizeObserver(() => chart?.resize())
  observer.observe(element.value)
})

watch(
  () => props.option,
  (option) => {
    chart?.setOption(option, true)
  },
  { deep: true },
)

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div
    ref="element"
    class="echart"
    :style="{ height: `${height}px` }"
    role="img"
    :aria-label="label"
  />
</template>

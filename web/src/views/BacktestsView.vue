<script setup lang="ts">
import EmptyState from '../components/EmptyState.vue'
</script>

<template>
  <div class="view">
    <header class="view-header">
      <h1>历史回测</h1>
      <p class="view-subtitle">时间划分、参数冻结、任务进度、比较表与逐期结果</p>
    </header>

    <EmptyState
      title="没有回测任务"
      description="回测需要先有数据快照与冻结的策略版本；运行前会预览计划与可评价期数。"
      hint="最早 60% 训练、随后 20% 验证、最后 20% 封存；封存测试只开放一次。"
    />

    <div class="panel">
      <h2>对照设计</h2>
      <ul class="plain-list">
        <li>B0：不加结构条件的均匀随机；B1：与策略施加相同条件的均匀随机。</li>
        <li>默认每个基线 200 条独立重复轨迹，与策略使用同一测试期集合与相同 K。</li>
        <li>主指标为每组平均红球命中数；区间含 0 时结论为“证据不足”。</li>
      </ul>
    </div>
  </div>
</template>

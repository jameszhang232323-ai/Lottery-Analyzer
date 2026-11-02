<script setup lang="ts">
import EmptyState from '../components/EmptyState.vue'
</script>

<template>
  <div class="view">
    <header class="view-header">
      <h1>统计分析</h1>
      <p class="view-subtitle">30 / 100 / 300 期与全范围：频次、遗漏、和值、奇偶、分区</p>
    </header>

    <EmptyState
      title="没有可分析的数据快照"
      description="统计需要先有开奖数据；每个结果都会标注窗口、样本数、截止期与公式版本。"
      hint="红球期频率之和为 6，蓝球为 1；不足样本的窗口不补成满样本。"
    />

    <div class="panel">
      <h2>口径提示</h2>
      <ul class="plain-list">
        <li>当前遗漏：从截止期向前连续未出现的期数；窗口内从未出现记为“至少 n 期”。</li>
        <li>冷热分组只描述历史出现情况，不解释为未来涨跌。</li>
        <li>图表数字必须与后端事实集一致，前端不自行计算。</li>
      </ul>
    </div>
  </div>
</template>

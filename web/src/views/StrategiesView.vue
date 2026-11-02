<script setup lang="ts">
import EmptyState from '../components/EmptyState.vue'
</script>

<template>
  <div class="view">
    <header class="view-header">
      <h1>策略实验</h1>
      <p class="view-subtitle">S0 均匀随机、S1 平滑频率、结构条件与候选表</p>
    </header>

    <EmptyState
      title="尚无策略版本"
      description="选择方法与参数后保存草稿，加入回测前会显示参数溯源与样本口径。"
      hint="S1 参数白名单：窗口 30/100/300/all，α 10/30/100，λ 0/0.1/0.25。"
    />

    <div class="panel">
      <h2>候选规则</h2>
      <ul class="plain-list">
        <li>每组 6 个互异红球（1—33）与 1 个蓝球（1—16），批内去重。</li>
        <li>结构条件先做必要条件检查，再有界拒绝抽样，最多 100,000 次尝试。</li>
        <li>相同快照、版本、参数与种子必须得到完全相同的结果。</li>
      </ul>
    </div>
  </div>
</template>

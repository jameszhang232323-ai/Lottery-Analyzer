<script setup lang="ts">
import EmptyState from '../components/EmptyState.vue'
</script>

<template>
  <div class="view">
    <header class="view-header">
      <h1>总览</h1>
      <p class="view-subtitle">最新开奖、数据质量、已锁定批次与近期实验</p>
    </header>

    <div class="card-grid">
      <EmptyState title="暂无开奖数据" description="尚未初始化数据源，最新结果会在采集完成后显示。"
        hint="入口：开奖数据 → 初始化五年数据。" />
      <EmptyState title="数据质量未评估" description="完整性与核验等级取决于已取得的期号清单和来源证据。" />
      <EmptyState title="没有已锁定批次" description="开奖前锁定的候选批次会在这里出现，锁定后核心内容不可修改。" />
      <EmptyState title="暂无实验摘要" description="策略实验与回测结果完成后方可汇总，不展示示例成绩。" />
    </div>
  </div>
</template>

<script setup lang="ts">
import EmptyState from '../components/EmptyState.vue'
</script>

<template>
  <div class="view">
    <header class="view-header">
      <h1>开奖数据</h1>
      <p class="view-subtitle">期号与日期筛选、号码表、来源详情、修订时间线、缺期列表</p>
    </header>

    <EmptyState
      title="尚未获取任何开奖记录"
      description="初始化五年数据后，这里按期号展示红球、蓝球、来源与核验级别。"
      hint="缺失奖金属“未取得”，不以 0 代替；冲突期暂停正式结算。"
    />

    <div class="panel">
      <h2>计划中的同步动作</h2>
      <ul class="plain-list">
        <li>初始化五年历史（按来源分页，断点续传）</li>
        <li>补漏与最近 30 期复核</li>
        <li>历史全量滚动复核（每 30 天）</li>
      </ul>
      <p class="muted">以上动作尚未接入接口，页面不提供伪造的进度或示例数据。</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import EmptyState from '../components/EmptyState.vue'
</script>

<template>
  <div class="view">
    <header class="view-header">
      <h1>候选记录</h1>
      <p class="view-subtitle">草稿、锁定、待开奖、已核对与修订待重算</p>
    </header>

    <EmptyState
      title="没有候选批次"
      description="在策略实验中生成候选并保存后，批次会出现在这里；锁定需要目标期日历与截止证据。"
      hint="锁定后核心内容不可修改，只能追加说明或归档。"
    />

    <div class="panel">
      <h2>锁定与核对规则</h2>
      <ul class="plain-list">
        <li>锁定使用后端/数据库时间，客户端时间不参与裁定。</li>
        <li>未知截止时间、目标期已有结果或快照包含目标期时，禁止正式前瞻锁定。</li>
        <li>同一批次与同一开奖修订只结算一次；更正后追加新结算，旧结算仍可查。</li>
      </ul>
    </div>
  </div>
</template>

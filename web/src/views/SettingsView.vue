<script setup lang="ts">
import { useSystemStore } from '../stores/system'

const system = useSystemStore()
</script>

<template>
  <div class="view">
    <header class="view-header">
      <h1>设置与任务</h1>
      <p class="view-subtitle">数据源、AI、费用限制、备份、版本、日志与任务队列</p>
    </header>

    <div class="panel">
      <h2>后端连通性</h2>
      <dl class="kv">
        <dt>状态</dt>
        <dd>{{ system.status }}</dd>
        <dt>彩种</dt>
        <dd>{{ system.games.map((game) => `${game.name}${game.enabled ? '' : '（预留）'}`).join('、') || '—' }}</dd>
        <dt>最近错误</dt>
        <dd>{{ system.lastError || '—' }}</dd>
      </dl>
      <button type="button" class="ghost-button" @click="system.refresh()">重新检测</button>
    </div>

    <div class="panel">
      <h2>尚未实现</h2>
      <ul class="plain-list">
        <li>数据源与 AI 凭证配置：只接受后端保存，界面仅显示掩码与连接状态。</li>
        <li>备份与恢复：管理员操作，恢复前需预览哈希并进入维护模式。</li>
        <li>任务队列：展示排队、运行、失败与取消；关闭浏览器不停止任务。</li>
      </ul>
      <p class="muted">当前版本不含任何真实凭证输入框，避免误存密钥。</p>
    </div>
  </div>
</template>

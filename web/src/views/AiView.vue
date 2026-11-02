<script setup lang="ts">
import EmptyState from '../components/EmptyState.vue'
</script>

<template>
  <div class="view">
    <header class="view-header">
      <h1>AI 分析</h1>
      <p class="view-subtitle">条件区、事实结果、AI 报告与引用、发送资料预览</p>
    </header>

    <EmptyState
      title="尚未配置 AI 服务"
      description="管理员在设置中填写服务凭证后，这里可预览发送内容并发起分析。"
      hint="AI 只输出结构化建议；数字由后端事实集填充，非法建议不可应用。"
    />

    <div class="panel">
      <h2>安全边界</h2>
      <ul class="plain-list">
        <li>浏览器不接触服务密钥，凭证只由后端读取并只返回掩码。</li>
        <li>发送内容默认仅含统计摘要与必要证据，不含本地路径与完整数据库。</li>
        <li>AI 不可用时，统计、候选与回测照常可用。</li>
      </ul>
    </div>
  </div>
</template>

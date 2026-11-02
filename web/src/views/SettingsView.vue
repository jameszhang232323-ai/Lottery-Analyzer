<script setup lang="ts">
import { computed } from 'vue'

import DataStatus from '../components/DataStatus.vue'
import EmptyState from '../components/EmptyState.vue'
import SectionCard from '../components/SectionCard.vue'
import StatusPill from '../components/StatusPill.vue'
import { formatSyncTime } from '../stores/selectors'
import { useSystemStore } from '../stores/system'

const system = useSystemStore()

const gameSummary = computed(() => {
  if (system.games.length === 0) return null
  return system.games.map((game) => `${game.name}${game.enabled ? '' : '（预留）'}`).join('、')
})

const checkedAtLabel = computed(() => formatSyncTime(system.checkedAt))

const errorKindLabel = computed(() => {
  switch (system.errorKind) {
    case 'network':
      return '网络不可达'
    case 'backend':
      return '后端错误'
    case 'unexpected':
      return '响应异常'
    default:
      return '无'
  }
})
</script>

<template>
  <div class="view">
    <header class="view-header">
      <div>
        <h1>设置与任务</h1>
        <p class="view-subtitle">数据源、AI、费用限制、备份、版本、日志与任务队列。</p>
      </div>
      <StatusPill :tone="system.backendStatus.tone" :label="system.backendStatus.label" />
    </header>

    <p class="view-scope">
      <span class="view-scope-label">口径</span>
      凭证只显示掩码，写入由后端认证加密保存；浏览器资源、localStorage 与日志均不含服务密钥。
      本页不提供任何密钥输入框，避免误存凭证。
    </p>

    <SectionCard
      title="后端连通性"
      description="健康探针与彩种清单为真实接口"
      tag="/api/v1"
      tag-tone="info"
    >
      <div class="data-status-grid">
        <DataStatus
          label="连接状态"
          :value="system.backendStatus.label"
          :tone="system.backendStatus.tone"
        />
        <DataStatus
          label="彩种"
          :value="gameSummary"
          :unwired="gameSummary === null"
        />
        <DataStatus
          label="最近检测"
          :value="checkedAtLabel"
          :unwired="system.checkedAt === null"
          note="Asia/Shanghai"
        />
        <DataStatus
          label="错误类别"
          :value="errorKindLabel"
          :tone="system.errorKind ? 'error' : 'ok'"
        />
        <DataStatus
          label="最近错误"
          :value="system.lastError || '无'"
          :tone="system.lastError ? 'error' : 'ok'"
        />
      </div>
      <p class="muted">{{ system.backendStatus.detail }}</p>
      <template #actions>
        <div class="action-row">
          <button
            type="button"
            class="action-button"
            :disabled="system.connection === 'checking'"
            @click="system.refresh()"
          >
            {{ system.connection === 'checking' ? '检测中…' : '重新检测后端' }}
          </button>
          <span class="muted">
            请求 GET /health/live、GET /health/ready、GET /games；失败时如实显示错误。
          </span>
        </div>
      </template>
    </SectionCard>

    <SectionCard title="数据源与 AI" description="来源能力、掩码与连接状态" tag="未接入" tag-tone="neutral">
      <EmptyState
        title="凭证配置未接入"
        description="管理员配置后，这里显示来源能力检测、掩码与连接状态，不显示明文。"
        hint="来源许可与覆盖范围未通过时，在线交付门槛保持未通过。"
      />
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" disabled title="后端接口未接入">
            测试数据源连接
          </button>
          <button type="button" class="action-button" disabled title="后端接口未接入">
            测试 AI 连接
          </button>
          <span class="unwired-note">后端接口未接入</span>
        </div>
      </template>
    </SectionCard>

    <SectionCard title="费用限制与备份" description="AI 调用上限、预算与备份清单">
      <div class="data-status-grid">
        <DataStatus label="每日调用上限" unwired />
        <DataStatus label="预算上限" unwired />
        <DataStatus label="最近备份" unwired />
        <DataStatus label="校验结果" unwired />
      </div>
      <EmptyState
        title="暂无备份记录"
        description="备份包含一致性数据库与被引用附件，不含服务密钥、主密钥、有效会话和临时缓存。"
        hint="恢复前先预览校验清单，并在维护模式下切换。"
      />
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" disabled title="后端接口未接入">
            创建备份
          </button>
          <button type="button" class="action-button" disabled title="后端接口未接入">
            恢复预览
          </button>
          <span class="unwired-note">后端接口未接入</span>
        </div>
      </template>
    </SectionCard>

    <SectionCard title="任务队列" description="排队、运行、失败与取消">
      <div class="status-count-grid">
        <DataStatus label="排队" unwired />
        <DataStatus label="运行" unwired />
        <DataStatus label="失败" unwired />
        <DataStatus label="取消" unwired />
      </div>
      <EmptyState
        title="暂无可读取的任务"
        description="任务状态由后端持久化队列提供，关闭或刷新浏览器不停止任务。"
        hint="耗时请求返回 job_id 与状态地址，前端按 2 秒轮询。"
      />
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" disabled title="后端接口未接入">
            刷新任务清单
          </button>
          <span class="unwired-note">后端接口未接入</span>
        </div>
      </template>
    </SectionCard>

    <SectionCard title="版本与日志" description="前端版本、接口前缀与脱敏日志">
      <dl class="kv">
        <dt>前端版本</dt>
        <dd>0.1.0</dd>
        <dt>接口前缀</dt>
        <dd>/api/v1</dd>
        <dt>连通方式</dt>
        <dd>同源 Cookie；不向浏览器下发密钥</dd>
        <dt>日志</dt>
        <dd>—</dd>
      </dl>
      <EmptyState
        title="暂无脱敏日志"
        description="后端接入后在管理员权限下展示脱敏日志，不包含凭证与请求头。"
      />
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" disabled title="后端接口未接入">
            查看脱敏日志
          </button>
          <span class="unwired-note">后端接口未接入</span>
        </div>
      </template>
    </SectionCard>
  </div>
</template>

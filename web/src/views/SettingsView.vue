<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  apiDownload,
  apiGet,
  apiPost,
  clientError,
  toApiError,
  type ApiError,
} from '../api/client'
import type {
  AdminUserResponse,
  AdminUsersResponse,
  Backup,
  BackupResponse,
  ExportResponse,
  RestorePreview,
  RestorePreviewResponse,
  ProviderStatus,
  ProvidersResponse,
  SecretReference,
  SecretsResponse,
  UserInfo,
  UserRole,
} from '../api/types'
import DataStatus from '../components/DataStatus.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorNote from '../components/ErrorNote.vue'
import SectionCard from '../components/SectionCard.vue'
import StatusPill from '../components/StatusPill.vue'
import { useAuthStore } from '../stores/auth'
import { useCandidatesStore } from '../stores/candidates'
import { useJobsStore } from '../stores/jobs'
import { useSystemStore } from '../stores/system'
import {
  describeJobStatus,
  formatCount,
  formatSyncTime,
  roleLabel,
} from '../stores/selectors'
import { saveBlob } from '../utils/download'

const auth = useAuthStore()
const system = useSystemStore()
const jobs = useJobsStore()
const candidates = useCandidatesStore()

const refreshing = ref(false)

const users = ref<UserInfo[]>([])
const usersLoading = ref(false)
const usersError = ref<ApiError | null>(null)
const newUsername = ref('')
const newPassword = ref('')
const newDisplayName = ref('')
const newRole = ref<UserRole>('analyst')
const creatingUser = ref(false)
const createUserError = ref<ApiError | null>(null)
const createUserNotice = ref('')
const disablingId = ref<number | null>(null)
const disableError = ref<ApiError | null>(null)

const secrets = ref<SecretReference[]>([])
const secretsLoading = ref(false)
const secretsError = ref<ApiError | null>(null)

const providers = ref<ProviderStatus[]>([])
const providersLoading = ref(false)
const providersError = ref<ApiError | null>(null)

const backup = ref<Backup | null>(null)
const backupLoading = ref(false)
const backupError = ref<ApiError | null>(null)
const previewBackupId = ref('')
const preview = ref<RestorePreview | null>(null)
const previewLoading = ref(false)
const previewError = ref<ApiError | null>(null)

const exportBatchInput = ref('')
const exportFormat = ref<'csv' | 'json' | 'html'>('csv')
const exporting = ref(false)
const downloading = ref(false)
const exportResult = ref<ExportResponse | null>(null)
const exportError = ref<ApiError | null>(null)

const roleOptions: Array<{ value: UserRole; label: string }> = [
  { value: 'analyst', label: '分析员' },
  { value: 'viewer', label: '访客' },
  { value: 'admin', label: '管理员' },
]

const checkedAtLabel = computed(() => formatSyncTime(system.checkedAt))
const errorKindLabel = computed(() => {
  switch (system.errorKind) {
    case 'network':
      return '网络不可达'
    case 'backend':
      return '后端错误'
    case 'unexpected':
      return '响应异常'
    case 'client':
      return '输入问题'
    default:
      return '无'
  }
})
const jobCounts = computed(() => {
  const counts: Record<string, number> = { queued: 0, running: 0, failed: 0, cancelled: 0 }
  for (const job of jobs.jobs) {
    counts[job.status] = (counts[job.status] ?? 0) + 1
  }
  return counts
})

async function refresh(): Promise<void> {
  refreshing.value = true
  await system.refreshAll('ssq', true)
  refreshing.value = false
}

async function loadUsers(): Promise<void> {
  usersLoading.value = true
  usersError.value = null
  try {
    const payload = await apiGet<AdminUsersResponse>('/admin/users')
    users.value = payload.items
  } catch (caught) {
    usersError.value = toApiError(caught)
  } finally {
    usersLoading.value = false
  }
}

async function createUser(): Promise<void> {
  createUserError.value = null
  createUserNotice.value = ''
  if (!newUsername.value.trim() || !newPassword.value) {
    createUserError.value = clientError('用户名和初始密码不能为空')
    return
  }
  creatingUser.value = true
  try {
    const payload = await apiPost<AdminUserResponse>('/admin/users', {
      username: newUsername.value.trim(),
      password: newPassword.value,
      display_name: newDisplayName.value.trim() || null,
      role: newRole.value,
    })
    createUserNotice.value = `已创建用户 ${payload.user.username}（${roleLabel(payload.user.role)}）`
    newUsername.value = ''
    newPassword.value = ''
    newDisplayName.value = ''
    await loadUsers()
  } catch (caught) {
    createUserError.value = toApiError(caught)
  } finally {
    creatingUser.value = false
  }
}

async function disableUser(user: UserInfo): Promise<void> {
  disableError.value = null
  if (typeof window !== 'undefined' && !window.confirm(`确认禁用用户 ${user.username} 并撤销其全部会话？`)) {
    return
  }
  disablingId.value = user.id
  try {
    await apiPost<AdminUserResponse>(`/admin/users/${user.id}/disable`)
    await loadUsers()
  } catch (caught) {
    disableError.value = toApiError(caught)
  } finally {
    disablingId.value = null
  }
}

async function loadSecrets(): Promise<void> {
  secretsLoading.value = true
  secretsError.value = null
  try {
    const payload = await apiGet<SecretsResponse>('/admin/secrets')
    secrets.value = payload.items
  } catch (caught) {
    secretsError.value = toApiError(caught)
  } finally {
    secretsLoading.value = false
  }
}

async function loadProviders(): Promise<void> {
  providersLoading.value = true
  providersError.value = null
  try {
    const payload = await apiGet<ProvidersResponse>('/admin/providers')
    providers.value = payload.items
  } catch (caught) {
    providersError.value = toApiError(caught)
  } finally {
    providersLoading.value = false
  }
}

function providerDetail(item: ProviderStatus): string {
  if (item.kind === 'ai') {
    if (!item.configured) return '—'
    const parts = [item.model, item.base_url, item.masked_key].filter(
      (value): value is string => Boolean(value),
    )
    return parts.join(' · ') || '—'
  }
  return item.name ? `${item.name}${item.license_verified ? '（已准入）' : '（未准入）'}` : '—'
}

async function createBackup(): Promise<void> {
  backupError.value = null
  backupLoading.value = true
  try {
    const payload = await apiPost<BackupResponse>('/admin/backups')
    backup.value = payload.backup
    previewBackupId.value = payload.backup.backup_id
  } catch (caught) {
    backupError.value = toApiError(caught)
  } finally {
    backupLoading.value = false
  }
}

async function previewRestore(): Promise<void> {
  previewError.value = null
  if (!previewBackupId.value.trim()) {
    previewError.value = clientError('恢复预览需要备份 ID')
    return
  }
  previewLoading.value = true
  try {
    const payload = await apiPost<RestorePreviewResponse>('/admin/restores/preview', {
      backup_id: previewBackupId.value.trim(),
    })
    preview.value = payload.preview
  } catch (caught) {
    previewError.value = toApiError(caught)
  } finally {
    previewLoading.value = false
  }
}

async function createExport(): Promise<void> {
  exportError.value = null
  const id = Number(exportBatchInput.value.trim())
  if (!Number.isInteger(id) || id < 1) {
    exportError.value = clientError('导出需要本人候选批次号')
    return
  }
  exporting.value = true
  try {
    exportResult.value = await apiPost<ExportResponse>('/exports', {
      batch_id: id,
      format: exportFormat.value,
    })
  } catch (caught) {
    exportError.value = toApiError(caught)
    exportResult.value = null
  } finally {
    exporting.value = false
  }
}

async function downloadExport(): Promise<void> {
  if (!exportResult.value) return
  downloading.value = true
  exportError.value = null
  try {
    const artifact = exportResult.value.artifact
    const file = await apiDownload(`/artifacts/${artifact.id}/download`)
    saveBlob(file.blob, file.filename || artifact.filename)
  } catch (caught) {
    exportError.value = toApiError(caught)
  } finally {
    downloading.value = false
  }
}

function useSessionBatch(id: number): void {
  exportBatchInput.value = String(id)
}

onMounted(() => {
  if (auth.isAdmin) {
    void loadUsers()
    void loadSecrets()
    void loadProviders()
  }
  void system.refresh()
})
</script>

<template>
  <div class="view">
    <header class="view-header">
      <div>
        <h1>设置与任务</h1>
        <p class="view-subtitle">后端连通性、用户、备份预览、导出与任务队列。</p>
      </div>
      <StatusPill :tone="system.backendStatus.tone" :label="system.backendStatus.label" />
    </header>

    <p class="view-scope">
      <span class="view-scope-label">口径</span>
      凭证只由后端加密保存并只返回掩码；浏览器资源、localStorage 与日志均不含服务密钥。
      本页不提供数据源/AI 密钥输入框，避免误存凭证。
    </p>

    <SectionCard title="后端连通性" description="健康探针、彩种与开奖覆盖为真实接口" tag="/api/v1" tag-tone="info">
      <div class="data-status-grid">
        <DataStatus label="连接状态" :value="system.backendStatus.label" :tone="system.backendStatus.tone" />
        <DataStatus label="彩种数" :value="system.games.length" />
        <DataStatus label="最近检测" :value="checkedAtLabel" :unwired="system.checkedAt === null" />
        <DataStatus label="最近同步" :value="'—'" :note="'后端未提供同步历史接口'" />
        <DataStatus
          label="错误类别"
          :value="errorKindLabel"
          :tone="system.errorKind ? 'error' : 'ok'"
        />
        <DataStatus label="有效期数" :value="formatCount(system.dataset.validDrawCount)" :unwired="system.dataset.validDrawCount === null" />
      </div>
      <p class="muted">{{ system.backendStatus.detail }}</p>
      <ErrorNote :error="system.lastApiError" context="读取后端状态" />
      <template #actions>
        <button type="button" class="action-button" :disabled="refreshing" @click="refresh">
          {{ refreshing ? '检测中…' : '重新检测后端' }}
        </button>
      </template>
    </SectionCard>

    <template v-if="auth.isAdmin">
      <SectionCard
        title="数据源与 AI 状态"
        description="GET /admin/providers；只返回是否配置与密钥掩码，不返回密钥本身"
        wide
      >
        <p v-if="providersLoading" class="loading-note">正在读取配置状态…</p>
        <ErrorNote :error="providersError" context="读取配置状态" />
        <div class="table-shell">
          <table>
            <thead>
              <tr>
                <th>类型</th>
                <th>状态</th>
                <th>详情</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in providers" :key="item.id">
                <td>{{ item.kind === 'ai' ? 'AI 服务' : '数据源' }}</td>
                <td>
                  <StatusPill
                    :label="item.configured ? '已配置' : '未配置'"
                    :tone="item.configured ? 'ok' : 'warn'"
                  />
                </td>
                <td>{{ providerDetail(item) }}</td>
                <td>{{ item.note ?? '—' }}</td>
              </tr>
              <tr v-if="!providers.length && !providersLoading">
                <td colspan="4" class="muted">暂无配置状态</td>
              </tr>
            </tbody>
          </table>
        </div>
      </SectionCard>

      <SectionCard title="用户管理" description="GET/POST /admin/users；禁用会撤销会话" wide>
        <p v-if="usersLoading" class="loading-note">正在读取用户列表…</p>
        <ErrorNote :error="usersError" context="读取用户" />
        <div class="table-shell">
          <table class="data-table">
            <thead>
              <tr>
                <th scope="col">ID</th>
                <th scope="col">用户名</th>
                <th scope="col">显示名</th>
                <th scope="col">角色</th>
                <th scope="col">状态</th>
                <th scope="col">创建时间</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="user in users" :key="user.id">
                <td>{{ user.id }}</td>
                <td>{{ user.username }}</td>
                <td>{{ user.display_name }}</td>
                <td>{{ roleLabel(user.role) }}</td>
                <td>
                  <StatusPill :tone="user.is_active ? 'ok' : 'neutral'" :label="user.is_active ? '启用' : '已禁用'" />
                </td>
                <td>{{ formatSyncTime(user.created_at) }}</td>
                <td>
                  <button
                    v-if="user.is_active && user.id !== auth.user?.id"
                    type="button"
                    class="ghost-button"
                    :disabled="disablingId === user.id"
                    @click="disableUser(user)"
                  >
                    {{ disablingId === user.id ? '禁用中…' : '禁用' }}
                  </button>
                  <span v-else class="muted">—</span>
                </td>
              </tr>
              <tr v-if="users.length === 0">
                <td class="table-empty" colspan="7">没有用户数据</td>
              </tr>
            </tbody>
          </table>
        </div>
        <ErrorNote :error="disableError" context="禁用用户" />
        <h3 class="subheading">创建用户</h3>
        <div class="filter-grid">
          <label class="field">
            <span class="field-label">用户名</span>
            <input v-model="newUsername" class="field-input" type="text" autocomplete="off" maxlength="64" />
          </label>
          <label class="field">
            <span class="field-label">初始密码</span>
            <input v-model="newPassword" class="field-input" type="password" autocomplete="new-password" />
          </label>
          <label class="field">
            <span class="field-label">显示名（可选）</span>
            <input v-model="newDisplayName" class="field-input" type="text" maxlength="64" />
          </label>
          <label class="field">
            <span class="field-label">角色</span>
            <select v-model="newRole" class="field-input">
              <option v-for="option in roleOptions" :key="option.value" :value="option.value">
                {{ option.label }}
              </option>
            </select>
          </label>
        </div>
        <p class="muted">密码只提交给后端，成功后立即清空输入框；页面不保存密码。</p>
        <p v-if="createUserNotice" class="ok-text">{{ createUserNotice }}</p>
        <ErrorNote :error="createUserError" context="创建用户" />
        <template #actions>
          <button type="button" class="action-button" :disabled="creatingUser" @click="createUser">
            {{ creatingUser ? '创建中…' : '创建用户' }}
          </button>
        </template>
      </SectionCard>

      <SectionCard title="密钥引用" description="GET /admin/secrets；只显示掩码" wide>
        <p v-if="secretsLoading" class="loading-note">正在读取密钥引用…</p>
        <ErrorNote :error="secretsError" context="读取密钥" />
        <div v-if="secrets.length" class="table-shell">
          <table class="data-table">
            <thead>
              <tr>
                <th scope="col">名称</th>
                <th scope="col">归属</th>
                <th scope="col">掩码</th>
                <th scope="col">密钥版本</th>
                <th scope="col">更新时间</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="secret in secrets" :key="secret.id">
                <td>{{ secret.name }}</td>
                <td>{{ secret.owner_id ?? '全局' }}</td>
                <td><code>{{ secret.masked_hint }}</code></td>
                <td>{{ secret.key_version }}</td>
                <td>{{ formatSyncTime(secret.updated_at) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <EmptyState
          v-else
          title="没有密钥引用"
          description="管理员写入后，此处只显示掩码；后端没有数据源/AI 连接测试接口，页面不伪造测试结果。"
          hint="凭证明文、密文与 nonce 永不返回浏览器。"
        />
      </SectionCard>

      <SectionCard title="备份与恢复预览" description="POST /admin/backups 与 /admin/restores/preview" wide>
        <div class="data-status-grid">
          <DataStatus label="最近备份" :value="backup?.backup_id ?? '—'" :unwired="backup === null" />
          <DataStatus label="大小" :value="backup ? `${backup.size_bytes} B` : '—'" :unwired="backup === null" />
          <DataStatus label="SHA-256" :value="backup?.sha256.slice(0, 16) ?? '—'" :unwired="backup === null" />
          <DataStatus label="创建时间" :value="formatSyncTime(backup?.created_at)" :unwired="backup === null" />
        </div>
        <p class="muted">
          后端没有备份列表接口，这里只显示本次会话创建的结果；恢复确认接口未在本页提供，避免误切换数据库。
        </p>
        <ErrorNote :error="backupError" context="创建备份" />
        <div class="filter-grid">
          <label class="field">
            <span class="field-label">恢复预览备份 ID</span>
            <input v-model="previewBackupId" class="field-input" type="text" placeholder="backups/ 目录下的归档名" />
          </label>
        </div>
        <p v-if="previewLoading" class="loading-note">正在隔离恢复预览…</p>
        <ErrorNote :error="previewError" context="恢复预览" />
        <div v-if="preview" class="data-status-grid">
          <DataStatus label="预览哈希" :value="preview.preview_hash.slice(0, 16)" />
          <DataStatus label="备份 schema" :value="preview.backup_schema ?? '—'" />
          <DataStatus label="应用版本" :value="preview.app_version ?? '—'" />
          <DataStatus label="归档 SHA-256" :value="preview.archive_sha256.slice(0, 16)" />
          <DataStatus label="会话清除" :value="preview.secrets_cleared ? '是' : '否'" />
          <DataStatus label="警告数" :value="preview.warnings.length" />
        </div>
        <details v-if="preview" class="raw-details">
          <summary>查看预览计数</summary>
          <pre>{{ JSON.stringify(preview.table_row_counts, null, 2) }}</pre>
        </details>
        <template #actions>
          <div class="action-row">
            <button type="button" class="action-button" :disabled="backupLoading" @click="createBackup">
              {{ backupLoading ? '备份中…' : '创建备份' }}
            </button>
            <button type="button" class="action-button" :disabled="previewLoading" @click="previewRestore">
              {{ previewLoading ? '预览中…' : '恢复预览' }}
            </button>
          </div>
        </template>
      </SectionCard>
    </template>

    <SectionCard v-else title="管理员功能" description="用户、密钥与备份仅管理员可用" wide>
      <EmptyState
        title="当前账号不是管理员"
        description="后端对 /admin/* 返回 403 ACCESS_DENIED；页面不会提前发起管理员请求。"
        hint="数据源与 AI 配置也由管理员在服务端维护。"
      />
    </SectionCard>

    <SectionCard title="导出" description="POST /exports 后用鉴权 fetch 下载附件">
      <div class="filter-grid">
        <label class="field">
          <span class="field-label">候选批次号（本人）</span>
          <input v-model="exportBatchInput" class="field-input" type="text" inputmode="numeric" placeholder="candidates id" />
        </label>
        <label class="field">
          <span class="field-label">格式</span>
          <select v-model="exportFormat" class="field-input">
            <option value="csv">CSV</option>
            <option value="json">JSON</option>
            <option value="html">HTML</option>
          </select>
        </label>
      </div>
      <div v-if="candidates.batches.length" class="action-row">
        <span class="muted">本会话批次：</span>
        <button
          v-for="batch in candidates.batches"
          :key="batch.id"
          type="button"
          class="ghost-button"
          @click="useSessionBatch(batch.id)"
        >
          #{{ batch.id }}
        </button>
      </div>
      <div v-if="exportResult" class="data-status-grid">
        <DataStatus label="附件号" :value="exportResult.artifact.id" />
        <DataStatus label="文件名" :value="exportResult.artifact.filename" />
        <DataStatus label="格式" :value="exportResult.format" />
        <DataStatus label="报告哈希" :value="exportResult.report_hash.slice(0, 16)" />
      </div>
      <p class="muted">
        下载地址为 GET /artifacts/{id}/download；请求携带同源 Cookie，令牌不放 URL。
      </p>
      <ErrorNote :error="exportError" context="导出" />
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" :disabled="exporting" @click="createExport">
            {{ exporting ? '生成中…' : '生成导出' }}
          </button>
          <button
            type="button"
            class="action-button"
            :disabled="!exportResult || downloading"
            @click="downloadExport"
          >
            {{ downloading ? '下载中…' : '下载附件' }}
          </button>
        </div>
      </template>
    </SectionCard>

    <SectionCard title="任务队列" description="本会话任务；后端没有全局任务清单接口">
      <div class="status-count-grid">
        <DataStatus label="排队" :value="jobCounts.queued ?? 0" />
        <DataStatus label="运行" :value="jobCounts.running ?? 0" />
        <DataStatus label="失败" :value="jobCounts.failed ?? 0" />
        <DataStatus label="取消" :value="jobCounts.cancelled ?? 0" />
      </div>
      <div v-if="jobs.jobs.length" class="table-shell">
        <table class="data-table">
          <thead>
            <tr>
              <th scope="col">任务</th>
              <th scope="col">类型</th>
              <th scope="col">状态</th>
              <th scope="col">进度</th>
              <th scope="col">错误码</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="job in jobs.jobs" :key="job.id">
              <td>#{{ job.id }} · {{ job.label }}</td>
              <td>{{ job.kind }}</td>
              <td>
                <StatusPill
                  :tone="describeJobStatus(job.status).tone"
                  :label="describeJobStatus(job.status).label"
                />
              </td>
              <td>{{ jobs.progressOf(job).text }}</td>
              <td>{{ job.errorCode ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <EmptyState
        v-else
        title="暂无可读取的任务"
        description="任务状态由后端持久化队列提供；底部任务中心每 2 秒轮询运行中的任务。"
        hint="关闭或刷新浏览器不停止后端任务；页面刷新后可按任务号恢复。"
      />
    </SectionCard>

    <SectionCard title="版本与边界" description="前端版本、接口前缀与未提供能力">
      <dl class="kv">
        <dt>前端版本</dt>
        <dd>0.1.0</dd>
        <dt>接口前缀</dt>
        <dd>/api/v1</dd>
        <dt>会话</dt>
        <dd>同源 HttpOnly Cookie；写请求带 X-CSRF-Token</dd>
        <dt>未提供</dt>
        <dd>任务清单、候选列表、AI 配置状态、数据源/AI 连接测试、备份列表</dd>
      </dl>
      <p class="muted">
        这些能力未接入时页面显示“后端未提供”，不使用本地数据或占位值替代。
        AI 每日调用上限与预算没有查询接口，页面不显示推测值；AI 配额错误会在任务失败时由后端返回真实错误码（当前任务详情接口尚未透出）。
      </p>
    </SectionCard>
  </div>
</template>

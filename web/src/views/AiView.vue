<script setup lang="ts">
import DataStatus from '../components/DataStatus.vue'
import EmptyState from '../components/EmptyState.vue'
import SectionCard from '../components/SectionCard.vue'
import StatusPill from '../components/StatusPill.vue'
</script>

<template>
  <div class="view">
    <header class="view-header">
      <div>
        <h1>AI 分析</h1>
        <p class="view-subtitle">条件区、事实结果、AI 报告与引用、发送资料预览。</p>
      </div>
      <StatusPill tone="neutral" label="AI 接口未接入" />
    </header>

    <p class="view-scope">
      <span class="view-scope-label">口径</span>
      AI 只输出结构化建议，观察项必须引用后端事实 ID；号码、命中与回测结果直接引用引擎结果，
      不采信 AI 自行编写的数字。未经校验的建议不可应用。
    </p>

    <SectionCard title="条件区" description="研究问题、彩种与统计窗口" tag="未接入" tag-tone="neutral">
      <div class="filter-grid">
        <label class="field">
          <span class="field-label">彩种</span>
          <select class="field-input" disabled>
            <option>双色球</option>
            <option>福彩3D（预留）</option>
          </select>
        </label>
        <label class="field">
          <span class="field-label">统计窗口</span>
          <select class="field-input" disabled>
            <option>100 期</option>
            <option>30 期</option>
            <option>300 期</option>
            <option>全范围</option>
          </select>
        </label>
      </div>
      <label class="field">
        <span class="field-label">研究问题</span>
        <textarea class="field-input" rows="3" placeholder="输入研究问题（AI 接口未接入）" disabled></textarea>
      </label>
      <p class="muted">发送前会预览资料范围；每日调用次数与费用上限由后端设置约束。</p>
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" disabled title="后端接口未接入">
            发起分析
          </button>
          <button type="button" class="action-button" disabled title="后端接口未接入">
            停止
          </button>
          <span class="unwired-note">后端接口未接入</span>
        </div>
      </template>
    </SectionCard>

    <SectionCard title="发送资料预览" description="AnalysisEvidenceV1 的发送范围">
      <div class="data-status-grid">
        <DataStatus label="快照哈希" unwired />
        <DataStatus label="截止期" unwired />
        <DataStatus label="有效样本" unwired />
        <DataStatus label="事实数量" unwired />
      </div>
      <EmptyState
        title="没有可预览的资料包"
        description="资料包由后端在固定快照上生成，默认只含统计摘要与必要证据。"
        hint="不发送凭证、本地路径、完整数据库或未选择的个人备注。"
      />
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" disabled title="后端接口未接入">
            预览发送范围
          </button>
          <span class="unwired-note">后端接口未接入</span>
        </div>
      </template>
    </SectionCard>

    <SectionCard title="事实结果" description="带 fact_id 的数值、单位与样本">
      <EmptyState
        title="暂无事实结果"
        description="每个事实包含唯一 ID、数值、单位、样本数、窗口与指标版本。"
        hint="界面上的数字从事实仓库填入，不由 AI 文本提供。"
      />
    </SectionCard>

    <SectionCard
      title="AI 报告与引用"
      description="观察、策略建议、限制与校验结果"
      tag="待验证"
      tag-tone="neutral"
      wide
    >
      <div class="data-status-grid">
        <DataStatus label="协议版本" value="AiReportV1" />
        <DataStatus label="引用事实" unwired />
        <DataStatus label="校验结果" unwired />
        <DataStatus label="用量记录" unwired />
      </div>
      <EmptyState
        title="暂无 AI 报告"
        description="报告必须包含数据依据、历史观察、策略参数、引擎候选、对照证据与不确定性。"
        hint="AI 失败时保留后端已有结果；超时或丢响应标记未知，不自动重复计费调用。"
      />
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" disabled title="后端接口未接入">
            查看依据
          </button>
          <button type="button" class="action-button" disabled title="后端接口未接入">
            预览建议参数
          </button>
          <button type="button" class="action-button" disabled title="后端接口未接入">
            应用建议
          </button>
          <span class="unwired-note">后端接口未接入</span>
        </div>
      </template>
    </SectionCard>

    <SectionCard title="使用边界" description="AI 可用性不影响 Python 统计与回测" wide>
      <ul class="plain-list">
        <li>浏览器不接触服务密钥，凭证只由后端读取并只返回掩码。</li>
        <li>AI 输出经过 Schema、事实引用与参数白名单校验后才可应用。</li>
        <li>AI 不可用时，统计、策略草稿与已生成候选照常使用。</li>
      </ul>
    </SectionCard>
  </div>
</template>

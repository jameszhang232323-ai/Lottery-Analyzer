<script setup lang="ts">
import { RouterLink } from 'vue-router'

import DataStatus from '../components/DataStatus.vue'
import EmptyState from '../components/EmptyState.vue'
import SectionCard from '../components/SectionCard.vue'
import StatusPill from '../components/StatusPill.vue'
</script>

<template>
  <div class="view">
    <header class="view-header">
      <div>
        <h1>总览</h1>
        <p class="view-subtitle">最新结果、数据质量、已锁定批次与近期实验摘要。</p>
      </div>
      <StatusPill tone="neutral" label="数据接口未接入" />
    </header>

    <p class="view-scope">
      <span class="view-scope-label">口径</span>
      总览只汇总后端返回的真实快照；数据未初始化时引导配置，不以任何非真实的号码、成绩或收益填充页面。
    </p>

    <div class="section-grid">
      <SectionCard
        title="最新结果"
        description="截至期号、开奖日期与 6 红 1 蓝号码"
        tag="未接入"
        tag-tone="neutral"
      >
        <EmptyState
          title="暂无开奖结果"
          description="初始化五年数据并同步后，这里显示截至期号与当期号码。"
          hint="号码只来自后端已发布的当前修订，前端不自行拼装或补位。"
        />
        <div class="data-status-grid">
          <DataStatus label="截至期号" unwired />
          <DataStatus label="开奖日期" unwired />
          <DataStatus label="当期号码" unwired />
        </div>
        <template #actions>
          <div class="action-row">
            <button type="button" class="action-button" disabled title="后端接口未接入">
              同步数据
            </button>
            <button type="button" class="action-button" disabled title="后端接口未接入">
              继续任务
            </button>
            <RouterLink class="action-link" to="/analysis">进入分析</RouterLink>
            <span class="unwired-note">后端接口未接入</span>
          </div>
        </template>
      </SectionCard>

      <SectionCard
        title="数据质量"
        description="完整性、核验级别与最近同步"
        tag="待评估"
        tag-tone="neutral"
      >
        <div class="data-status-grid">
          <DataStatus label="有效期数" unwired />
          <DataStatus label="完整性" unwired />
          <DataStatus label="核验级别" unwired />
          <DataStatus label="最近同步" unwired />
        </div>
        <EmptyState
          title="尚无质量清单"
          description="完整性取决于官方日历、期号清单与实际取得期数，三类证据分开记录。"
          hint="未取得的奖金显示“未取得”，不以 0 代替；有冲突时暂停正式结算。"
        />
      </SectionCard>

      <SectionCard title="已锁定批次" description="开奖前锁定的候选批次与核对状态">
        <EmptyState
          title="没有已锁定批次"
          description="锁定需要目标期日历与截止证据，锁定后核心内容不可修改。"
          hint="锁定后的批次会进入预定义统计，不因事后改参而重写。"
        />
      </SectionCard>

      <SectionCard title="近期实验摘要" description="策略版本、回测状态与结论">
        <EmptyState
          title="暂无实验摘要"
          description="策略实验与回测完成后，按数据快照汇总指标与状态。"
          hint="尚未回测的策略只显示“待验证”，不显示为已验证优势。"
        />
      </SectionCard>

      <SectionCard
        title="首次使用"
        description="从配置到首个研究闭环"
        tag="引导"
        tag-tone="info"
        wide
      >
        <ol class="step-list">
          <li>确认数据来源与允许用途，只由后端保存凭证。</li>
          <li>预览五年范围与预计调用量，再创建初始化任务。</li>
          <li>查看完整性、缺期与冲突说明，按实际范围开始统计。</li>
          <li>完成一次统计、一次策略与随机对照、一次候选草稿保存。</li>
        </ol>
        <p class="muted">
          当前版本只接通健康探针与彩种清单；上述步骤的提交接口尚未接入，页面不提供伪造进度。
        </p>
        <template #actions>
          <div class="action-row">
            <button type="button" class="action-button" disabled title="后端接口未接入">
              初始化五年数据
            </button>
            <button type="button" class="action-button" disabled title="后端接口未接入">
              配置数据源
            </button>
            <span class="unwired-note">后端接口未接入</span>
          </div>
        </template>
      </SectionCard>
    </div>
  </div>
</template>

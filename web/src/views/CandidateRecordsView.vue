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
        <h1>候选记录</h1>
        <p class="view-subtitle">草稿、锁定、待开奖、已核对与修订待重算。</p>
      </div>
      <StatusPill tone="neutral" label="候选接口未接入" />
    </header>

    <p class="view-scope">
      <span class="view-scope-label">口径</span>
      锁定采用后端或数据库时间，客户端时间不参与裁定；锁定后核心内容不可修改，只能追加说明或归档。
      同一批次与同一开奖修订只结算一次，开奖更正后追加新结算，旧结算仍可查。
    </p>

    <SectionCard title="批次状态" description="按生命周期分组的真实数量" tag="未接入" tag-tone="neutral">
      <div class="status-count-grid">
        <DataStatus label="草稿" unwired />
        <DataStatus label="锁定" unwired />
        <DataStatus label="待开奖" unwired />
        <DataStatus label="已核对" unwired />
        <DataStatus label="修订待重算" unwired />
      </div>
      <p class="muted">数量由后端按当前账号可见范围统计，前端不根据本地记录推算。</p>
    </SectionCard>

    <SectionCard
      title="批次列表"
      description="目标期、候选组数、锁定时间与结算状态"
      tag="未接入"
      tag-tone="neutral"
      wide
    >
      <div class="table-shell">
        <table class="data-table">
          <caption class="muted">候选批次表：当前无数据</caption>
          <thead>
            <tr>
              <th scope="col">批次</th>
              <th scope="col">目标期</th>
              <th scope="col">候选组数</th>
              <th scope="col">状态</th>
              <th scope="col">锁定时间</th>
              <th scope="col">结算</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td class="table-empty" colspan="6">暂无候选批次</td>
            </tr>
          </tbody>
        </table>
      </div>
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" disabled title="后端接口未接入">
            查看原始批次
          </button>
          <button type="button" class="action-button" disabled title="后端接口未接入">
            导出
          </button>
          <span class="unwired-note">后端接口未接入</span>
        </div>
      </template>
    </SectionCard>

    <SectionCard title="核对详情" description="命中明细、奖级与结算修订">
      <div class="data-status-grid">
        <DataStatus label="开奖修订" unwired />
        <DataStatus label="命中明细" unwired />
        <DataStatus label="适用奖级" unwired />
        <DataStatus label="结算修订" unwired />
      </div>
      <EmptyState
        title="暂无核对记录"
        description="结果和规则足够时才计算命中与奖级；奖金资料不完整时保留未知，不假定金额。"
        hint="来源冲突不结算；规则未确认时只输出命中明细。"
      />
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" disabled title="后端接口未接入">
            开始核对
          </button>
          <button type="button" class="action-button" disabled title="后端接口未接入">
            追加结算修订
          </button>
          <span class="unwired-note">后端接口未接入</span>
        </div>
      </template>
    </SectionCard>

    <SectionCard title="锁定与统计集合" description="正式前瞻与探索成绩分开" wide>
      <ul class="plain-list">
        <li>目标期来自经过确认的日历，未知截止不允许进入正式前瞻状态。</li>
        <li>锁定时固定号码、目标期、快照、策略参数、算法版本、种子与时间。</li>
        <li>前瞻主成绩每个目标期每通道只取首个有效锁定批次，其他批次单列探索成绩。</li>
        <li>聚合展示目标期数、批次数、候选组数与排除数，不把重叠批次当作独立事件。</li>
      </ul>
    </SectionCard>
  </div>
</template>

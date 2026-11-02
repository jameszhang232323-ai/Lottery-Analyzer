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
        <h1>统计分析</h1>
        <p class="view-subtitle">30 / 100 / 300 期与全范围：频次、遗漏、和值、奇偶、分区。</p>
      </div>
      <StatusPill tone="neutral" label="统计接口未接入" />
    </header>

    <p class="view-scope">
      <span class="view-scope-label">口径</span>
      红球期频率之和为 6，蓝球为 1；当前遗漏从截止期向前连续未出现的实际开奖期数计算，
      窗口内从未出现记为“至少 n 期”。不足样本的窗口不补成满样本，图表数字必须与后端事实集一致。
    </p>

    <SectionCard title="窗口筛选" description="窗口与样本口径随结果一起返回" tag="未接入" tag-tone="neutral">
      <div class="filter-grid">
        <label class="field">
          <span class="field-label">统计窗口</span>
          <select class="field-input" disabled>
            <option>30 期</option>
            <option>100 期</option>
            <option>300 期</option>
            <option>全范围</option>
          </select>
        </label>
        <label class="field">
          <span class="field-label">对比窗口</span>
          <select class="field-input" disabled>
            <option>不对比</option>
            <option>30 期与 100 期</option>
            <option>100 期与 300 期</option>
          </select>
        </label>
        <label class="field">
          <span class="field-label">指标版本</span>
          <select class="field-input" disabled>
            <option>由后端返回</option>
          </select>
        </label>
      </div>
      <p class="muted">统计由后端 worker 计算并写入事实集，前端只渲染，不自行复制算法。</p>
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" disabled title="后端接口未接入">
            比较窗口
          </button>
          <button type="button" class="action-button" disabled title="后端接口未接入">
            创建统计任务
          </button>
          <span class="unwired-note">后端接口未接入</span>
        </div>
      </template>
    </SectionCard>

    <SectionCard
      title="频次与遗漏"
      description="红球 / 蓝球出现次数、期频率与当前遗漏"
      tag="未接入"
      tag-tone="neutral"
      wide
    >
      <div class="data-status-grid">
        <DataStatus label="口径" value="期频率" />
        <DataStatus label="有效样本" unwired />
        <DataStatus label="截止期" unwired />
        <DataStatus label="公式版本" unwired />
      </div>
      <EmptyState
        title="暂无频次与遗漏图表"
        description="统计任务完成后，按窗口展示 33 个红球与 16 个蓝球的事实数值。"
        hint="每张图都会标注窗口、样本数、截止期与公式版本；冷热名次不解释为未来涨跌。"
      />
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" disabled title="后端接口未接入">
            查看明细
          </button>
          <button type="button" class="action-button" disabled title="后端接口未接入">
            发送至 AI
          </button>
          <span class="unwired-note">后端接口未接入</span>
        </div>
      </template>
    </SectionCard>

    <SectionCard title="结构指标" description="和值、跨度、奇偶与三区个数">
      <div class="chart-placeholder">
        <p class="chart-placeholder-title">暂无结构指标图表</p>
        <p class="chart-placeholder-hint">和值、跨度、奇偶与分区按同一窗口与截止期计算</p>
      </div>
      <EmptyState
        title="暂无结构统计结果"
        description="结构指标需要与频次统计来自同一数据快照，避免窗口错配。"
        hint="样本不足 30 期时不下稳定性结论。"
      />
    </SectionCard>

    <SectionCard title="多窗口比较" description="30 / 100 / 300 期与全范围并列">
      <EmptyState
        title="暂无可比较的窗口"
        description="每个窗口分别计算并保留有效样本，不把不足样本的窗口补成满样本。"
        hint="窗口切换不会覆盖旧任务结果，旧结果标记为对应窗口。"
      />
    </SectionCard>
  </div>
</template>

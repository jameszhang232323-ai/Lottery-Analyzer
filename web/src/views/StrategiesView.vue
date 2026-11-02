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
        <h1>策略实验</h1>
        <p class="view-subtitle">S0 均匀随机、S1 平滑频率、结构条件与候选表。</p>
      </div>
      <StatusPill tone="neutral" label="候选接口未接入" />
    </header>

    <p class="view-scope">
      <span class="view-scope-label">口径</span>
      S1 的抽样权重是候选生成权重，既不是号码的真实边际概率，也不是整组中奖概率；
      相同快照、引擎版本、参数、条件与种子必须得到完全相同的候选。
    </p>

    <SectionCard title="策略参数" description="方法与参数白名单" tag="未接入" tag-tone="neutral">
      <div class="filter-grid">
        <label class="field">
          <span class="field-label">方法</span>
          <select class="field-input" disabled>
            <option>S0 均匀随机（默认）</option>
            <option>S1 平滑频率混合</option>
          </select>
        </label>
        <label class="field">
          <span class="field-label">窗口</span>
          <select class="field-input" disabled>
            <option>100 期</option>
            <option>30 期</option>
            <option>300 期</option>
            <option>全范围</option>
          </select>
        </label>
        <label class="field">
          <span class="field-label">等效先验期数 α</span>
          <select class="field-input" disabled>
            <option>30</option>
            <option>10</option>
            <option>100</option>
          </select>
        </label>
        <label class="field">
          <span class="field-label">混合系数 λ</span>
          <select class="field-input" disabled>
            <option>0.1</option>
            <option>0</option>
            <option>0.25</option>
          </select>
        </label>
        <label class="field">
          <span class="field-label">候选组数</span>
          <input class="field-input" type="number" value="5" min="1" max="20" disabled />
        </label>
      </div>
      <div class="data-status-grid">
        <DataStatus label="算法版本" value="sha256-counter-v1" />
        <DataStatus label="参数版本" unwired />
        <DataStatus label="批内去重" value="是" />
        <DataStatus label="种子来源" unwired />
      </div>
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" disabled title="后端接口未接入">
            保存草稿
          </button>
          <button type="button" class="action-button" disabled title="后端接口未接入">
            生成候选
          </button>
          <span class="unwired-note">后端接口未接入</span>
        </div>
      </template>
    </SectionCard>

    <SectionCard title="结构条件" description="条件无解时提示具体条件，不静默放宽" tag="可选" tag-tone="info">
      <div class="checkbox-row">
        <label><input type="checkbox" disabled /> 限制奇数个数</label>
        <label><input type="checkbox" disabled /> 限制和值区间</label>
        <label><input type="checkbox" disabled /> 限制三区个数</label>
        <label><input type="checkbox" disabled /> 限制最大连号对数</label>
      </div>
      <div class="filter-grid">
        <label class="field">
          <span class="field-label">固定红球</span>
          <input class="field-input" type="text" placeholder="格式由后端校验" disabled />
        </label>
        <label class="field">
          <span class="field-label">排除红球</span>
          <input class="field-input" type="text" placeholder="格式由后端校验" disabled />
        </label>
      </div>
      <p class="muted">
        固定红球不超过 6 个，固定与排除不得冲突，三区个数之和必须为 6；拒绝抽样最多 100,000 次尝试。
      </p>
    </SectionCard>

    <SectionCard
      title="候选表"
      description="目标期、号码、种子与批次状态"
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
              <th scope="col">方法</th>
              <th scope="col">红球</th>
              <th scope="col">蓝球</th>
              <th scope="col">状态</th>
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
            加入回测
          </button>
          <button type="button" class="action-button" disabled title="后端接口未接入">
            锁定批次
          </button>
          <span class="unwired-note">后端接口未接入</span>
        </div>
      </template>
    </SectionCard>

    <SectionCard title="随机对照入口" description="与策略相同的结构条件与候选组数">
      <EmptyState
        title="尚无随机对照任务"
        description="B0 为不加条件的均匀随机，B1 为施加相同条件的均匀随机。"
        hint="生成不足时保留实际产出数，不静默改变参数或重复补足。"
      />
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" disabled title="后端接口未接入">
            生成随机对照
          </button>
          <span class="unwired-note">后端接口未接入</span>
        </div>
      </template>
    </SectionCard>
  </div>
</template>

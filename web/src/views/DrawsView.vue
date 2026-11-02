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
        <h1>开奖数据</h1>
        <p class="view-subtitle">期号与日期筛选、号码表、来源详情、修订时间线与缺期列表。</p>
      </div>
      <StatusPill tone="neutral" label="数据接口未接入" />
    </header>

    <p class="view-scope">
      <span class="view-scope-label">口径</span>
      期号保存为字符串并按年份与序号规范化排序；红球为 6 个互异整数（1—33），蓝球 1 个（1—16）。
      缺失奖金显示“未取得”，不以 0 代替；冲突期暂停正式结算并保留原修订。
    </p>

    <SectionCard title="筛选" description="按期号或开奖日期缩小范围" tag="未接入" tag-tone="neutral">
      <div class="filter-grid">
        <label class="field">
          <span class="field-label">期号</span>
          <input class="field-input" type="text" placeholder="按期号筛选" disabled />
        </label>
        <label class="field">
          <span class="field-label">开始日期</span>
          <input class="field-input" type="date" disabled />
        </label>
        <label class="field">
          <span class="field-label">结束日期</span>
          <input class="field-input" type="date" disabled />
        </label>
        <label class="field">
          <span class="field-label">核验级别</span>
          <select class="field-input" disabled>
            <option>全部级别</option>
            <option>单源结构通过</option>
            <option>跨源一致</option>
            <option>官方已核对</option>
          </select>
        </label>
      </div>
      <p class="muted">筛选与分页由后端 /draws 提供，当前版本尚未接入。</p>
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" disabled title="后端接口未接入">
            查询
          </button>
          <button type="button" class="action-button" disabled title="后端接口未接入">
            重置
          </button>
          <span class="unwired-note">后端接口未接入</span>
        </div>
      </template>
    </SectionCard>

    <SectionCard
      title="开奖号码表"
      description="按期号倒序展示号码、来源与核验级别"
      tag="未接入"
      tag-tone="neutral"
      wide
    >
      <div class="ball-legend">
        <span class="ball ball--red">红</span>
        <span class="ball-caption">红球 1—33，每期 6 个</span>
        <span class="ball ball--blue">蓝</span>
        <span class="ball-caption">蓝球 1—16，每期 1 个</span>
      </div>
      <div class="table-shell">
        <table class="data-table">
          <caption class="muted">开奖记录表：当前无数据</caption>
          <thead>
            <tr>
              <th scope="col">期号</th>
              <th scope="col">开奖日期</th>
              <th scope="col">红球</th>
              <th scope="col">蓝球</th>
              <th scope="col">来源</th>
              <th scope="col">核验级别</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td class="table-empty" colspan="6">暂无开奖记录</td>
            </tr>
          </tbody>
        </table>
      </div>
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" disabled title="后端接口未接入">
            初始化五年数据
          </button>
          <button type="button" class="action-button" disabled title="后端接口未接入">
            补漏
          </button>
          <button type="button" class="action-button" disabled title="后端接口未接入">
            复核最近 30 期
          </button>
          <button type="button" class="action-button" disabled title="后端接口未接入">
            导出
          </button>
          <span class="unwired-note">后端接口未接入</span>
        </div>
      </template>
    </SectionCard>

    <SectionCard title="来源详情" description="每个来源的观察、版本与核验证据">
      <div class="data-status-grid">
        <DataStatus label="来源" unwired />
        <DataStatus label="来源版本" unwired />
        <DataStatus label="最近同步" unwired />
        <DataStatus label="核验级别" unwired />
      </div>
      <EmptyState
        title="暂无来源记录"
        description="同步成功后按来源展示原始观察引用、取得时间与字段映射结果；冲突行可回到各来源逐项核对。"
        hint="跨源一致不等于独立核验，更不等同于官方核对。"
      />
    </SectionCard>

    <SectionCard title="修订时间线" description="若同一期出现更正，按修订号追加">
      <EmptyState
        title="暂无修订记录"
        description="号码、日期、奖金或特别规定状态变化时，各自记录原因并生成新修订。"
        hint="同一彩种与期号只有一个当前有效修订，旧修订与旧报告保留可查。"
      />
    </SectionCard>

    <SectionCard title="缺期列表" description="缺失、休市与延期分别说明">
      <div class="data-status-grid">
        <DataStatus label="预计期数" unwired />
        <DataStatus label="已取得" unwired />
        <DataStatus label="缺失" unwired />
        <DataStatus label="冲突" unwired />
      </div>
      <EmptyState
        title="暂无缺期清单"
        description="缺期清单需要官方日历、休市公告与已确认期号目录的组合依据。"
        hint="只有范围可信且无未解决缺期时，才显示“该范围数据完整”。"
      />
    </SectionCard>
  </div>
</template>

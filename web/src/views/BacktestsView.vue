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
        <h1>历史回测</h1>
        <p class="view-subtitle">时间划分、参数冻结记录、任务进度、比较表与逐期结果。</p>
      </div>
      <StatusPill tone="neutral" label="回测接口未接入" />
    </header>

    <p class="view-scope">
      <span class="view-scope-label">口径</span>
      按日期升序取最早 60% 训练、随后 20% 验证、最后 20% 封存，边界按完整开奖期取整；
      t 期候选只读取序号小于 t 的输入。主指标为每组平均红球命中数，区间含 0 时结论为证据不足。
    </p>

    <SectionCard title="时间划分与参数冻结" description="计划、冻结与封存状态" tag="未接入" tag-tone="neutral">
      <div class="data-status-grid">
        <DataStatus label="训练段" value="60%" />
        <DataStatus label="验证段" value="20%" />
        <DataStatus label="封存段" value="20%" />
        <DataStatus label="冻结计划" unwired />
      </div>
      <EmptyState
        title="尚无冻结的回测计划"
        description="预览计划时会列出三段实际可评价期数、热身期与排除原因。"
        hint="封存测试只开放一次；据封存结果继续调参后，原封存集转为已使用数据。"
      />
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" disabled title="后端接口未接入">
            预览计划
          </button>
          <button type="button" class="action-button" disabled title="后端接口未接入">
            冻结参数
          </button>
          <span class="unwired-note">后端接口未接入</span>
        </div>
      </template>
    </SectionCard>

    <SectionCard title="任务进度" description="阶段、完成量与最近检查点">
      <div class="data-status-grid">
        <DataStatus label="任务状态" unwired />
        <DataStatus label="当前阶段" unwired />
        <DataStatus label="最近检查点" unwired />
        <DataStatus label="取消状态" unwired />
      </div>
      <EmptyState
        title="没有运行中的回测任务"
        description="回测由后端 worker 分批计算并保存检查点，关闭浏览器不停止任务。"
        hint="取消请求只写标记，worker 确认后才显示为已取消；部分结果不混为完整报告。"
      />
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" disabled title="后端接口未接入">
            运行回测
          </button>
          <button type="button" class="action-button" disabled title="后端接口未接入">
            取消任务
          </button>
          <span class="unwired-note">后端接口未接入</span>
        </div>
      </template>
    </SectionCard>

    <SectionCard
      title="比较表"
      description="策略与 B0 / B1 的同口径对照"
      tag="未接入"
      tag-tone="neutral"
      wide
    >
      <div class="data-status-grid">
        <DataStatus label="主指标" value="每组平均红球命中数" />
        <DataStatus label="B0 轨迹" unwired />
        <DataStatus label="B1 轨迹" unwired />
        <DataStatus label="95% 区间" unwired />
      </div>
      <EmptyState
        title="暂无比较结果"
        description="对照要求每期候选数量、去重规则、允许信息与可评价期集合一致。"
        hint="报告同时展示快照、版本、排除原因与完整/取消状态；探索记录与封存结果分开标记。"
      />
      <template #actions>
        <div class="action-row">
          <button type="button" class="action-button" disabled title="后端接口未接入">
            导出报告
          </button>
          <span class="unwired-note">后端接口未接入</span>
        </div>
      </template>
    </SectionCard>

    <SectionCard title="逐期结果" description="按期展示候选、命中与排除原因">
      <EmptyState
        title="暂无逐期结果"
        description="每个可评价期保存候选哈希、命中明细、基线结果与排除原因。"
        hint="排除期数及原因可展开；取消任务不混入完整结果。"
      />
    </SectionCard>
  </div>
</template>

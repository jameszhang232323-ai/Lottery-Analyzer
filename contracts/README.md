# 接口契约（v1）

前端、后端与 AI 之间的数据形状。约定：

- 契约先于实现：新增字段只在 v1 内向后兼容地添加，破坏性改动升 v2 并保留 v1 样例。
- 金额用整数分，计数用整数，时间用 UTC ISO 8601，开奖日期用 `YYYY-MM-DD`。
- 期号是字符串（`"2026014"`），排序按年份与序号。
- 所有 ID、号码、次数均为结构约束，不提供真实开奖数据。

| 文件 | 用途 | 对应设计 |
|---|---|---|
| [draw-revision.schema.json](draw-revision.schema.json) | 一期开奖的一次修订（双色球/3D） | 6.1、6.2 |
| [analysis-evidence.schema.json](analysis-evidence.schema.json) | 发送给 AI 的 `AnalysisEvidenceV1` | 8.2 |
| [ai-report.schema.json](ai-report.schema.json) | AI 返回的 `AiReportV1` | 8.3 |

服务端校验在任务 09 实现；当前 JSON Schema 用于评审与样例对照，不作为在线接口。

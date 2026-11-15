/**
 * 计划草稿 → 确认入参。
 *
 * 这一步看起来只是"把字段抄一遍"，但抄错一个字段的代价是**任务断了溯源链**：
 * `source_rule_code` 不回传，任务落库后查不回是哪条规则；`due_source` 不回传，
 * 「这个日期来自政策原文」就再也证明不了；`needs_human_confirmation` 不回传，
 * 「需人工确认日期」的标记会消失，用户会以为日期是确定的。
 *
 * 所以它只能有一份实现。原先这段映射写在 `MatchView.vue` 里；R13 的推荐卡片
 * 也要"调用现有计划生成能力"，两边各写一遍必然漂移——抽到这里，两处共用。
 */

import type { DraftData, DraftTask } from '../api/client'

export interface ConfirmTaskPayload {
  temp_id: string
  title: string
  description: string | null
  priority: string
  due_at: string | null
  required_materials: string | null
  dependency_ids: string[]
  source_rule_code: string | null
  due_source: string | null
  note: string | null
  needs_human_confirmation: boolean
}

export interface ConfirmPlanPayload {
  title: string
  tasks: ConfirmTaskPayload[]
}

function taskOf(task: DraftTask): ConfirmTaskPayload {
  return {
    temp_id: task.temp_id,
    title: task.title,
    description: task.description,
    priority: task.priority,
    due_at: task.due_at,
    required_materials: task.required_materials,
    dependency_ids: task.dependency_ids,
    source_rule_code: task.source_rule_code,
    due_source: task.due_source,
    note: task.note,
    needs_human_confirmation: task.needs_human_confirmation,
  }
}

export function toConfirmPayload(draft: DraftData): ConfirmPlanPayload {
  return { title: draft.title, tasks: draft.tasks.map(taskOf) }
}

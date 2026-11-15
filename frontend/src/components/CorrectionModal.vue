<script setup lang="ts">
/**
 * 纠错反馈弹层（R16）——政策详情 / 指南 / 匹配结果三处共用的入口。
 *
 * 只做一件事：把"哪里写错了"收下来，交给 `POST /feedback/corrections`。
 * 学生反馈**不改任何正式政策内容**——后端那张表只记"谁、对什么、说了什么"。
 *
 * 三个页面各传一组定位：
 *   target_type ∈ document / guide / match；target_id = 对应主键 / 指南 key / 匹配会话 id。
 */
import { ref, watch } from 'vue'
import { api, type CorrectionTarget, type CorrectionSubmitResult } from '../api/client'
import { FEEDBACK_REASON_LABEL } from '../utils/format'

const props = defineProps<{
  open: boolean
  targetType: CorrectionTarget
  targetId: string
  documentId: string | null
  /** 展示用：正在纠错的是哪份政策 / 哪个事项 */
  targetTitle: string
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'submitted'): void
}>()

const reason = ref('')
const comment = ref('')
const busy = ref(false)
const error = ref('')
const done = ref(false)

const reasons = Object.keys(FEEDBACK_REASON_LABEL)

watch(() => props.open, (open) => {
  if (open) {
    reason.value = ''
    comment.value = ''
    error.value = ''
    done.value = false
  }
})

async function submit() {
  const text = comment.value.trim()
  if (!text) {
    error.value = '请先说明具体哪里写错了，管理员才能核对'
    return
  }
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await api.post<CorrectionSubmitResult>('/feedback/corrections', {
      target_type: props.targetType,
      target_id: props.targetId,
      document_id: props.documentId,
      reason_code: reason.value || null,
      comment: text,
    })
    done.value = true
    emit('submitted')
  } catch (err) {
    error.value = err instanceof Error ? err.message : '提交失败，请稍后再试'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div v-if="open" class="corr-overlay" @click.self="emit('close')">
    <div class="corr-dialog card" role="dialog" aria-modal="true">
      <h3>纠错反馈</h3>
      <p class="muted small">
        正在反馈：<strong>{{ targetTitle }}</strong>
      </p>

      <template v-if="done">
        <p class="ok-line">已提交，感谢你的反馈。管理员核对后会按需更正。</p>
        <div class="corr-actions">
          <button class="btn" @click="emit('close')">关闭</button>
        </div>
      </template>

      <template v-else>
        <label class="corr-field">
          <span>问题类型（可选）</span>
          <select v-model="reason" class="select">
            <option value="">不确定 / 不选</option>
            <option v-for="code in reasons" :key="code" :value="code">
              {{ FEEDBACK_REASON_LABEL[code] }}
            </option>
          </select>
        </label>

        <label class="corr-field">
          <span>具体哪里错了（必填）</span>
          <textarea
            v-model="comment"
            class="input"
            rows="4"
            maxlength="1000"
            placeholder="例如：这份政策的发布日期写的是 2024 年，但学校官网已是 2026 年版…"
          ></textarea>
        </label>

        <p v-if="error" class="err-line">{{ error }}</p>

        <div class="corr-actions">
          <button class="btn btn-ghost" :disabled="busy" @click="emit('close')">取消</button>
          <button class="btn" :disabled="busy" @click="submit">
            {{ busy ? '提交中…' : '提交反馈' }}
          </button>
        </div>
        <p class="muted tiny">
          你的反馈不会直接改动政策内容，仅用于管理员核对与更正。
        </p>
      </template>
    </div>
  </div>
</template>

<style scoped>
.corr-overlay {
  position: fixed;
  inset: 0;
  background: rgba(18, 48, 43, 0.32);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 70;
  padding: var(--space-4);
}
.corr-dialog {
  width: min(460px, 100%);
  box-shadow: var(--shadow-pop);
}
.corr-dialog h3 {
  margin: 0 0 4px;
}
.corr-field {
  display: block;
  margin-top: var(--space-3);
}
.corr-field > span {
  display: block;
  font-size: 11.5px;
  color: var(--c-text-2);
  margin-bottom: 4px;
}
.corr-field .input {
  width: 100%;
  box-sizing: border-box;
}
textarea.input {
  padding: var(--space-2);
  resize: vertical;
}
.corr-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  margin-top: var(--space-3);
}
.ok-line {
  color: var(--c-met);
  margin: var(--space-3) 0 0;
}
</style>

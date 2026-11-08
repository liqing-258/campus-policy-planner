<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, type Health } from '../api/client'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const health = ref<Health | null>(null)
const loading = ref(true)

onMounted(async () => {
  try {
    health.value = await api.get<Health>('/health')
  } finally {
    loading.value = false
  }
})

const milestones = [
  { id: 'M2', title: '工程可跑', detail: '应用工厂 · 登录 · 健康检查 · 建表脚本', done: true },
  { id: 'M3', title: '知识库可用', detail: '上传 · 解析 · 切分 · 索引', done: false },
  { id: 'M4', title: '问答可信', detail: '混合检索 · 结构化答案 · 拒答', done: false },
  { id: 'M5', title: '闭环可演', detail: '规则抽取 · 三态匹配 · 计划 · 任务', done: false },
]
</script>

<template>
  <div class="grid">
    <section class="card welcome">
      <h2>你好，{{ auth.displayName }}</h2>
      <p class="muted">
        这是 M2 阶段的工作台骨架。真实数据区（待办、最近问答、政策库状态）会在 M3–M5 接入，
        当前不显示任何占位假数据——<strong>没有数据就显示"暂无"</strong>，不编造。
      </p>
    </section>

    <section class="card">
      <h3>系统状态</h3>
      <dl v-if="!loading && health" class="kv">
        <dt>版本</dt><dd class="mono">{{ health.version }}</dd>
        <dt>数据库</dt><dd>{{ health.db }}</dd>
        <dt>模型模式</dt>
        <dd>
          <span :class="['pill', health.llm_mode]">{{ health.llm_mode }}</span>
        </dd>
        <dt>检索模式</dt><dd class="mono">{{ health.embedding_mode }}</dd>
        <dt>政策文件</dt><dd>{{ health.document_count }} 份</dd>
        <dt>文本片段</dt><dd>{{ health.chunk_count }} 条</dd>
      </dl>
      <p v-else class="muted">读取中…</p>
    </section>

    <section class="card milestones">
      <h3>里程碑进度</h3>
      <ol>
        <li v-for="m in milestones" :key="m.id" :class="{ done: m.done }">
          <span class="badge">{{ m.id }}</span>
          <div>
            <strong>{{ m.title }}</strong>
            <div class="muted small">{{ m.detail }}</div>
          </div>
          <span class="state">{{ m.done ? '已完成' : '未开始' }}</span>
        </li>
      </ol>
    </section>

    <section class="card next">
      <h3>接下来</h3>
      <p class="muted">
        M3 将开放 <strong>政策知识库</strong>：管理员上传 PDF/DOCX，系统解析、切分并建立索引，
        学生端可浏览片段并定位原文。上传入口会随 M3 出现在左侧导航。
      </p>
    </section>
  </div>
</template>

<style scoped>
.grid {
  display: grid;
  grid-template-columns: 1.4fr 1fr;
  gap: var(--space-4);
}

.welcome {
  grid-column: 1 / -1;
}

.kv {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: var(--space-2) var(--space-4);
  margin: 0;
}

.kv dt {
  color: var(--c-text-2);
}

.kv dd {
  margin: 0;
}

.pill {
  display: inline-block;
  padding: 1px 10px;
  border-radius: 999px;
  font-size: 12px;
  border: 1px solid var(--c-border);
}

.pill.llm {
  color: var(--c-met);
  border-color: #cfe6da;
  background: #f1f8f4;
}

.pill.degraded {
  color: var(--c-unknown);
  border-color: #f0dfae;
  background: #fff8e6;
}

.milestones ol {
  list-style: none;
  margin: 0;
  padding: 0;
}

.milestones li {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: var(--space-3);
  align-items: center;
  padding: var(--space-2) 0;
  border-bottom: 1px dashed var(--c-border);
}

.milestones li:last-child {
  border-bottom: none;
}

.badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 24px;
  border-radius: 6px;
  background: var(--c-bg);
  border: 1px solid var(--c-border);
  font-size: 12px;
  font-family: "Cascadia Mono", monospace;
}

.milestones li.done .badge {
  background: var(--c-primary-weak);
  border-color: #c9dcf5;
  color: var(--c-primary);
}

.state {
  font-size: 12px;
  color: var(--c-text-2);
}

.milestones li.done .state {
  color: var(--c-met);
}

.small {
  font-size: 12px;
}

@media (max-width: 768px) {
  .grid {
    grid-template-columns: 1fr;
  }
}
</style>

<script setup lang="ts">
/**
 * 政策知识库（M3）。
 *
 * 三件事：看有哪些文件、看文件被切成了什么、管理员上传新文件。
 * 「片段」是问答与匹配的证据单位，所以它必须能被直接看到——
 * 只给一个"片段数 6"而不给内容，用户无从判断切分是否合理。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { api, type ChunkRow, type DocumentRow, type Paged } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { PARSE_STATUS, formatBytes, formatTime, parseStatus } from '../utils/format'

const auth = useAuthStore()

const docs = ref<DocumentRow[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 10
const keyword = ref('')
const statusFilter = ref('')
const loading = ref(false)
const error = ref('')

const chunks = ref<ChunkRow[]>([])
const chunkTotal = ref(0)
const openDocId = ref('')
const chunkLoading = ref(false)

const uploading = ref(false)
const uploadMsg = ref('')
const uploadError = ref('')
const fileInput = ref<HTMLInputElement | null>(null)

async function loadDocs() {
  loading.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({ page: String(page.value), page_size: String(pageSize) })
    if (keyword.value.trim()) params.set('q', keyword.value.trim())
    if (statusFilter.value) params.set('parse_status', statusFilter.value)
    const data = await api.get<Paged<DocumentRow>>(`/documents?${params}`)
    docs.value = data.items
    total.value = data.total
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function toggleChunks(doc: DocumentRow) {
  if (openDocId.value === doc.id) {
    openDocId.value = ''
    return
  }
  openDocId.value = doc.id
  chunkLoading.value = true
  chunks.value = []
  try {
    const data = await api.get<Paged<ChunkRow>>(
      `/documents/${doc.id}/chunks?page=1&page_size=50`,
    )
    chunks.value = data.items
    chunkTotal.value = data.total
  } catch (err) {
    error.value = err instanceof Error ? err.message : '片段加载失败'
  } finally {
    chunkLoading.value = false
  }
}

async function onUpload(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  uploading.value = true
  uploadMsg.value = ''
  uploadError.value = ''
  const form = new FormData()
  form.append('file', file)
  form.append('title', file.name.replace(/\.[^.]+$/, ''))
  try {
    const doc = await api.upload<DocumentRow>('/documents', form)
    uploadMsg.value = `已上传「${doc.title}」，解析状态：${parseStatus(doc.parse_status).label}`
    page.value = 1
    await loadDocs()
  } catch (err) {
    uploadError.value = err instanceof Error ? err.message : '上传失败'
  } finally {
    uploading.value = false
    if (fileInput.value) fileInput.value.value = ''
  }
}

async function removeDoc(doc: DocumentRow) {
  if (!window.confirm(`确认下架「${doc.title}」？这是软删除，记录仍可追溯。`)) return
  try {
    await api.remove(`/documents/${doc.id}`)
    if (openDocId.value === doc.id) openDocId.value = ''
    await loadDocs()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '删除失败'
  }
}

const pages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const statusOptions = Object.keys(PARSE_STATUS)

onMounted(loadDocs)
watch([page, statusFilter], loadDocs)

function search() {
  page.value = 1
  loadDocs()
}
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h2>政策知识库</h2>
        <p class="muted small">
          上传的政策文件在这里解析、切分并建立索引。片段是问答与匹配的证据单位，
          所以可以逐条打开看原文。
        </p>
      </div>
      <div class="head-actions">
        <input v-model="keyword" class="search" placeholder="按标题搜索" @keyup.enter="search" />
        <select v-model="statusFilter" class="select">
          <option value="">全部状态</option>
          <option v-for="s in statusOptions" :key="s" :value="s">
            {{ PARSE_STATUS[s].label }}
          </option>
        </select>
        <button class="btn btn-ghost" @click="search">搜索</button>
      </div>
    </header>

    <section v-if="auth.isAdmin" class="card upload-card">
      <h3>上传政策文件</h3>
      <p class="muted small">
        支持 PDF / DOCX / TXT / MD。扫描件（无文本层）会被识别为「无有效文本」而不是硬塞进索引——
        本系统不含 OCR。
      </p>
      <div class="upload-row">
        <button class="btn" :disabled="uploading" @click="fileInput?.click()">
          {{ uploading ? '上传中…' : '选择文件并上传' }}
        </button>
        <!-- 只保留这一个 input：绑两个 ref 会让 $refs 指向后一个，
             点击按钮时打开的是没有 change 处理的那个，上传永远不触发 -->
        <input
          ref="fileInput"
          type="file"
          accept=".pdf,.docx,.txt,.md"
          class="hidden-input"
          @change="onUpload"
        />
        <span class="muted tiny">支持 PDF / DOCX / TXT / MD</span>
      </div>
      <p v-if="uploadMsg" class="ok-line">{{ uploadMsg }}</p>
      <p v-if="uploadError" class="err-line">{{ uploadError }}</p>
    </section>

    <p v-else class="muted small">上传入口仅管理员可见。学生可浏览片段并定位原文。</p>

    <p v-if="error" class="err-line">{{ error }}</p>

    <section class="card">
      <div class="list-head">
        <h3>文件（{{ total }}）</h3>
        <span v-if="loading" class="muted small">加载中…</span>
      </div>

      <table class="table">
        <thead>
          <tr>
            <th>标题</th>
            <th>类型</th>
            <th>大小</th>
            <th>状态</th>
            <th class="num">片段</th>
            <th>上传时间</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <template v-for="doc in docs" :key="doc.id">
            <tr :class="{ active: openDocId === doc.id }">
              <td>
                <button class="link" @click="toggleChunks(doc)">{{ doc.title }}</button>
                <div v-if="doc.parse_hint" class="muted tiny">{{ doc.parse_hint }}</div>
              </td>
              <td class="mono">{{ doc.file_type }}</td>
              <td class="mono">{{ formatBytes(doc.file_size) }}</td>
              <td>
                <span :class="['tag', parseStatus(doc.parse_status).tone]">
                  {{ parseStatus(doc.parse_status).label }}
                </span>
              </td>
              <td class="num">{{ doc.chunk_count }}</td>
              <td class="muted small">{{ formatTime(doc.created_at) }}</td>
              <td>
                <button
                  v-if="auth.isAdmin"
                  class="link danger"
                  @click="removeDoc(doc)"
                >下架</button>
              </td>
            </tr>
            <tr v-if="openDocId === doc.id" class="detail-row">
              <td colspan="7">
                <div v-if="chunkLoading" class="muted small">片段加载中…</div>
                <div v-else-if="!chunks.length" class="muted small">该文件没有片段。</div>
                <div v-else class="chunk-list">
                  <div class="muted small">共 {{ chunkTotal }} 个片段，按顺序展示：</div>
                  <article v-for="chunk in chunks" :key="chunk.id" class="chunk">
                    <header>
                      <span class="tag plain">#{{ chunk.chunk_index + 1 }}</span>
                      <span class="muted small">
                        {{ chunk.section_title || '未识别章节' }}
                        <template v-if="chunk.page_number"> · 第 {{ chunk.page_number }} 页</template>
                      </span>
                    </header>
                    <p class="chunk-text">{{ chunk.content }}</p>
                  </article>
                </div>
              </td>
            </tr>
          </template>
          <tr v-if="!docs.length && !loading">
            <td colspan="7" class="muted empty">暂无文件。管理员可在上方上传。</td>
          </tr>
        </tbody>
      </table>

      <div v-if="pages > 1" class="pager">
        <button class="btn btn-ghost" :disabled="page <= 1" @click="page--">上一页</button>
        <span class="muted small">{{ page }} / {{ pages }}</span>
        <button class="btn btn-ghost" :disabled="page >= pages" @click="page++">下一页</button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.page-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--space-4);
  flex-wrap: wrap;
}

.head-actions {
  display: flex;
  gap: var(--space-2);
  align-items: center;
}

.search,
.select {
  min-height: 40px;
  padding: 0 var(--space-3);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-control);
  background: #fff;
  font-family: inherit;
  font-size: 14px;
  color: var(--c-text);
}

.small {
  font-size: 12px;
}

.tiny {
  font-size: 11px;
}

.upload-row {
  display: flex;
  gap: var(--space-3);
  align-items: center;
  flex-wrap: wrap;
  margin-top: var(--space-3);
}

.hidden-input {
  display: none;
}

.ok-line {
  color: var(--c-met);
  font-size: 13px;
  margin: var(--space-2) 0 0;
}

.err-line {
  color: var(--c-not-met);
  font-size: 13px;
  margin: var(--space-2) 0 0;
}

.list-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: var(--space-3);
}

.table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.table th {
  text-align: left;
  font-weight: 500;
  color: var(--c-text-2);
  padding: var(--space-2);
  border-bottom: 1px solid var(--c-border);
  white-space: nowrap;
}

.table td {
  padding: var(--space-2);
  border-bottom: 1px dashed var(--c-border);
  vertical-align: top;
}

.table tr.active td {
  background: var(--c-primary-weak);
}

.num {
  text-align: right;
}

.link {
  background: none;
  border: none;
  padding: 0;
  color: var(--c-primary);
  cursor: pointer;
  font-size: 13px;
  font-family: inherit;
  text-align: left;
}

.link.danger {
  color: var(--c-not-met);
}

.tag {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 12px;
  border: 1px solid var(--c-border);
  white-space: nowrap;
}

.tag.met {
  color: var(--c-met);
  border-color: #cfe6da;
  background: #f1f8f4;
}

.tag.unknown {
  color: var(--c-unknown);
  border-color: #f0dfae;
  background: #fff8e6;
}

.tag.not_met {
  color: var(--c-not-met);
  border-color: #f2d3d0;
  background: #fdf2f1;
}

.tag.plain {
  color: var(--c-text-2);
  background: var(--c-bg);
}

.detail-row td {
  background: #fbfcfd;
}

.chunk-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-2) 0;
}

.chunk {
  border-left: 3px solid var(--c-primary-weak);
  padding-left: var(--space-3);
}

.chunk header {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  margin-bottom: var(--space-1);
}

.chunk-text {
  margin: 0;
  white-space: pre-wrap;
  color: var(--c-text);
  font-size: 13px;
  line-height: 1.75;
}

.empty {
  text-align: center;
  padding: var(--space-6) 0;
}

.pager {
  display: flex;
  gap: var(--space-3);
  align-items: center;
  justify-content: center;
  padding-top: var(--space-4);
}

@media (max-width: 768px) {
  .table {
    display: block;
    overflow-x: auto;
  }
}
</style>

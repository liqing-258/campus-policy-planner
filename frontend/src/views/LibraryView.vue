<script setup lang="ts">
/**
 * 政策知识库（M3）。
 *
 * 三件事：看有哪些文件、看文件被切成了什么、管理员上传新文件。
 * 「片段」是问答与匹配的证据单位，所以它必须能被直接看到——
 * 只给一个"片段数 6"而不给内容，用户无从判断切分是否合理。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import {
  api,
  type ChunkRow,
  type DocumentRow,
  type DocumentVersions,
  type Paged,
  type VersionBrief,
  type VersionCandidates,
} from '../api/client'
import CorrectionModal from '../components/CorrectionModal.vue'
import FavoriteButton from '../components/FavoriteButton.vue'
import { useAuthStore } from '../stores/auth'
import {
  EFFECTIVE_LABEL,
  PARSE_STATUS,
  formatBytes,
  formatTime,
  parseStatus,
} from '../utils/format'

const auth = useAuthStore()
const route = useRoute()

const docs = ref<DocumentRow[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 10
const keyword = ref('')
const statusFilter = ref('')
const loading = ref(false)
const error = ref('')

/* R18：知识库概览——四个数都来自真实接口（/documents 全量），前端不自己猜 */
const overview = ref({ total: 0, parsed: 0, chunks: 0, pending: 0 })
// 「已成功解析」= 已切分/已索引/已解析；「待处理」= 上传后还没走到解析完成的状态
const READY_STATUSES = ['indexed', 'chunked', 'parsed']

const chunks = ref<ChunkRow[]>([])
const chunkTotal = ref(0)
const openDocId = ref('')
const chunkLoading = ref(false)

/* ---------------- R15：政策版本与更新提醒 ---------------- */
const docVersions = ref<DocumentVersions | null>(null)
const versionLoading = ref(false)
// 上传后"是否属于政策更新"的提示（后端 possible_updates 原样带出）
const uploadPrompt = ref<(VersionCandidates & { newId: string; newTitle: string }) | null>(null)
const linkForm = ref({ oldId: '', newStatus: 'active' })
const linking = ref(false)
const linkMsg = ref('')
const linkError = ref('')
// 相关文件 = 同发布部门的其他文件（真实关联；不做语义推荐）
const relatedDocs = ref<DocumentRow[]>([])
// R16 纠错反馈：详情页上的「报告此处有误」入口
const correction = ref<{ open: boolean; doc: DocumentRow | null }>({ open: false, doc: null })

const uploading = ref(false)
const uploadMsg = ref('')
const uploadError = ref('')
const fileInput = ref<HTMLInputElement | null>(null)

/**
 * 上传时的版本元数据。三列从 M3 建表起就在，接口也一直支持写入，
 * 但前端长期没有入口——演示库里 4 份文件这三项全是空，
 * 版本冲突检测（T-12）在这种数据上一个字都说不出来。
 *
 * 「有效状态」默认留空由后端存成 unknown，而不是替用户猜一个 active：
 * 猜错会让一份过期文件在冲突面板里被当成"现行有效"，比不标注更误导。
 */
const uploadMeta = ref({
  academic_year: '',
  published_at: '',
  effective_status: '',
})
const effectiveOptions = Object.keys(EFFECTIVE_LABEL)

function resetUploadMeta() {
  uploadMeta.value = { academic_year: '', published_at: '', effective_status: '' }
}

/**
 * 就地补录/修改版本信息。
 *
 * 上传时忘了标注，之前唯一的补救办法是删了重传——但重传会换 document_id，
 * 挂在这份文件上的已复核规则与历史引用全部作废（见后端 documents.py 的同名说明）。
 * 所以这里提供一个只改元数据、不动文件与片段的入口。
 */
const editingId = ref('')
const editForm = ref({ academic_year: '', published_at: '', effective_status: '' })
const editSaving = ref(false)
const editError = ref('')

function startEdit(doc: DocumentRow) {
  editingId.value = doc.id
  editError.value = ''
  editForm.value = {
    academic_year: doc.academic_year ?? '',
    published_at: doc.published_at ? doc.published_at.slice(0, 10) : '',
    effective_status: doc.effective_status || '',
  }
}

async function saveEdit(doc: DocumentRow) {
  editSaving.value = true
  editError.value = ''
  try {
    const updated = await api.patch<DocumentRow>(`/documents/${doc.id}`, {
      academic_year: editForm.value.academic_year.trim(),
      published_at: editForm.value.published_at,
      effective_status: editForm.value.effective_status || 'unknown',
    })
    // 就地替换而不是整页重拉：重拉会收起正在展开的片段列表，
    // 用户刚定位到的那条原文就没了。
    const index = docs.value.findIndex((d) => d.id === doc.id)
    if (index >= 0) docs.value[index] = updated
    editingId.value = ''
  } catch (err) {
    editError.value = err instanceof Error ? err.message : '保存失败'
  } finally {
    editSaving.value = false
  }
}

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

/** 顶部概览：拉一次全量列表（page_size 上限 100，演示库远小于此）算四个数。 */
async function loadOverview() {
  try {
    const all = await api.get<Paged<DocumentRow>>('/documents?page=1&page_size=100')
    const list = all.items
    overview.value = {
      total: all.total,
      parsed: list.filter((d) => READY_STATUSES.includes(d.parse_status)).length,
      chunks: list.reduce((sum, d) => sum + (d.chunk_count || 0), 0),
      pending: list.filter((d) => !READY_STATUSES.includes(d.parse_status)).length,
    }
  } catch {
    // 概览失败不阻断列表：列表自己会报错
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
  linkForm.value = { oldId: '', newStatus: 'active' }
  linkMsg.value = ''
  linkError.value = ''
  void loadVersions(doc.id)
  void loadRelated(doc)
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

/** 同一政策的全部版本（主题组 ∪ 人工 supersedes 链），详情页"历史版本"用。 */
async function loadVersions(docId: string) {
  versionLoading.value = true
  docVersions.value = null
  try {
    docVersions.value = await api.get<DocumentVersions>(`/documents/${docId}/versions`)
  } catch {
    docVersions.value = null
  } finally {
    versionLoading.value = false
  }
}

/**
 * 旧版本横幅：当前文件已被替代 / 不是最新版时给醒目提示。
 * 两件事必须分开说——「已被替代」是管理员确认过的事实，
 * 「不是最新」只是日期比较的推断。
 */
function versionBanner(doc: DocumentRow): { tone: 'warn' | 'info'; text: string } | null {
  const list = docVersions.value
  if (!list || list.count < 2 || list.document_id !== doc.id) return null
  const newest = list.versions.find((v) => v.document_id === list.newest_document_id)
  // 「已被替代」是管理员确认过的事实，**不依赖"是否最新"的日期推断**——
  // 第一版先判 newest，结果被日期排序短路：旧版标了已被替代却看不到横幅。
  if (doc.effective_status === 'superseded') {
    const who = newest && newest.document_id !== doc.id ? `；最新版本是《${newest.title}》` : ''
    return { tone: 'warn', text: `该文件已被替代，请勿按本文条款办理${who}。` }
  }
  if (!newest || newest.document_id === doc.id) return null
  return { tone: 'info', text: `该文件不是最新版本，最新的是《${newest.title}》——仅供查阅，请核对后再办理。` }
}

/** 打开另一个版本的详情：目标不一定在当前页，先定位页码再展开。 */
async function openVersion(v: VersionBrief) {
  await openDocById(v.document_id)
}

/** 管理员确认版本关联：一次做"设关联 + 旧版标已被替代 + 新版标有效状态"。 */
async function linkVersion() {
  if (!openDocId.value || !linkForm.value.oldId) return
  linking.value = true
  linkMsg.value = ''
  linkError.value = ''
  try {
    await api.post(`/documents/${openDocId.value}/version-link`, {
      supersedes_id: linkForm.value.oldId,
      new_status: linkForm.value.newStatus,
    })
    linkMsg.value = '版本关联已建立：旧版已标记「已被替代」。'
    await loadVersions(openDocId.value)
    await loadDocs()
  } catch (err) {
    linkError.value = err instanceof Error ? err.message : '关联失败'
  } finally {
    linking.value = false
  }
}

/** 同发布部门的其他文件（排除自己与同主题版本——它们在"历史版本"里已列出）。 */
async function loadRelated(doc: DocumentRow) {
  relatedDocs.value = []
  if (!doc.department) return
  try {
    const all = await api.get<Paged<DocumentRow>>('/documents?page=1&page_size=100')
    const familyIds = new Set((docVersions.value?.versions ?? []).map((v) => v.document_id))
    relatedDocs.value = all.items
      .filter((d) => d.id !== doc.id && d.department === doc.department && !familyIds.has(d.id))
      .slice(0, 5)
  } catch {
    relatedDocs.value = []
  }
}

/** 「关联历史版本」字段：supersedes_id 指向的那份文件的标题。 */
function linkedOldTitle(doc: DocumentRow): string {
  if (!doc.supersedes_id) return ''
  const hit = docVersions.value?.versions.find((v) => v.document_id === doc.supersedes_id)
  return hit ? hit.title : '已关联（对方不在当前家族列表）'
}

function dismissUploadPrompt() {
  uploadPrompt.value = null
}

/** 上传完成后的"是否政策更新"一键建联（新文件替代所选旧文件）。 */
async function confirmUploadLink() {
  const prompt = uploadPrompt.value
  if (!prompt || !linkForm.value.oldId) return
  linking.value = true
  linkError.value = ''
  try {
    await api.post(`/documents/${prompt.newId}/version-link`, {
      supersedes_id: linkForm.value.oldId,
      new_status: linkForm.value.newStatus,
    })
    uploadPrompt.value = null
    linkForm.value = { oldId: '', newStatus: 'active' }
    uploadMsg.value = '版本关联已建立：旧版已标记「已被替代」。'
    await loadDocs()
  } catch (err) {
    linkError.value = err instanceof Error ? err.message : '关联失败'
  } finally {
    linking.value = false
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
  // 空值不发：后端 optional_* 会把"没填"存成 NULL，发空串反而要走一遍校验。
  if (uploadMeta.value.academic_year.trim()) {
    form.append('academic_year', uploadMeta.value.academic_year.trim())
  }
  if (uploadMeta.value.published_at) {
    form.append('published_at', uploadMeta.value.published_at)
  }
  if (uploadMeta.value.effective_status) {
    form.append('effective_status', uploadMeta.value.effective_status)
  }
  try {
    const doc = await api.upload<DocumentRow & { possible_updates?: VersionCandidates }>(
      '/documents', form,
    )
    uploadMsg.value = `已上传「${doc.title}」，解析状态：${parseStatus(doc.parse_status).label}`
    // "是否属于政策更新"的提示点：同主题/高度相似的既有文件原样带出，
    // 由管理员确认——确认前不建联、不改任何人的有效状态
    const found = doc.possible_updates
    if (found && (found.strong.length || found.weak.length)) {
      uploadPrompt.value = {
        strong: found.strong,
        weak: found.weak,
        newId: doc.id,
        newTitle: doc.title,
      }
      linkForm.value = {
        oldId: (found.strong[0] ?? found.weak[0])?.document_id ?? '',
        newStatus: 'active',
      }
    } else {
      uploadPrompt.value = null
    }
    resetUploadMeta()
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

/**
 * 从问答页的「在知识库中打开」跳过来时带 `?doc=<id>`：
 * 把那一行滚到视野中央并展开它的片段，否则用户落地在一张列表上，
 * 还得自己找刚才引用的是哪一份。
 */
const highlightId = ref('')

async function openDocById(target: string) {
  if (!target) return
  highlightId.value = target
  statusFilter.value = ''
  keyword.value = ''
  // 列表按创建时间倒序分页，目标文件不一定落在第 1 页——
  // 演示库里最早导入的《奖学金评审办法》正好排在最后一页。
  // 先取一次全量 id 定位页码，比"逐页翻着试"可靠。
  try {
    const all = await api.get<Paged<DocumentRow>>('/documents?page=1&page_size=100')
    const index = all.items.findIndex((d) => d.id === target)
    page.value = index >= 0 ? Math.floor(index / pageSize) + 1 : 1
  } catch {
    page.value = 1
  }
  await loadDocs()
  const found = docs.value.find((d) => d.id === target)
  if (!found) return
  if (openDocId.value !== target) await toggleChunks(found)
  await nextTick()
  document.getElementById(`doc-${target}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

async function openFromQuery() {
  await openDocById(String(route.query.doc ?? ''))
}

// 挂载时必须先拉一次列表：`openFromQuery` 在没有 `?doc=` 时直接 return，
// 只挂它的话 `loadDocs` 永远不执行，页面会一直显示「文件（0）」——
// 而接口其实返回了 18 条。深链分支自己会调 `loadDocs`，别重复请求。
onMounted(async () => {
  const target = String(route.query.doc ?? '')
  if (target) {
    await openFromQuery()
    return
  }
  await loadDocs()
  void loadOverview()
})
watch([page, statusFilter], loadDocs)
watch(() => route.query.doc, openFromQuery)

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

    <!-- R18 知识库概览：四个数都来自 /documents 全量，不是前端拍脑袋 -->
    <div class="grid grid-4 overview-grid">
      <div class="stat accent">
        <span class="k">文件总数</span>
        <span class="v">{{ overview.total }}<small>份</small></span>
        <span class="d">知识库已收录</span>
      </div>
      <div class="stat ok">
        <span class="k">已成功解析</span>
        <span class="v">{{ overview.parsed }}<small>份</small></span>
        <span class="d">能检索到正文</span>
      </div>
      <div class="stat info">
        <span class="k">知识片段</span>
        <span class="v">{{ overview.chunks }}<small>个</small></span>
        <span class="d">问答与匹配的证据</span>
      </div>
      <div class="stat" :class="overview.pending > 0 ? 'warn' : ''">
        <span class="k">待处理</span>
        <span class="v">{{ overview.pending }}<small>份</small></span>
        <span class="d">{{ overview.pending > 0 ? '需关注解析状态' : '暂无待处理' }}</span>
      </div>
    </div>

    <section v-if="auth.isAdmin" class="card upload-card">
      <h3>上传政策文件</h3>
      <p class="muted small">
        支持 PDF / DOCX / TXT / MD。扫描件（无文本层）会被识别为「无有效文本」而不是硬塞进索引——
        本系统不含 OCR。
      </p>
      <div class="upload-grid">
        <label class="field">
          <span>学年</span>
          <input
            v-model="uploadMeta.academic_year"
            placeholder="如 2025-2026"
            class="meta-input"
          />
        </label>
        <label class="field">
          <span>发布日期</span>
          <input v-model="uploadMeta.published_at" type="date" class="meta-input" />
        </label>
        <label class="field">
          <span>有效状态</span>
          <select v-model="uploadMeta.effective_status" class="meta-input">
            <option value="">暂未确认（存入 unknown）</option>
            <option v-for="s in effectiveOptions" :key="s" :value="s">
              {{ EFFECTIVE_LABEL[s] }}
            </option>
          </select>
        </label>
      </div>
      <p class="muted tiny">
        这三项决定「同一政策有多个版本时能不能提示冲突」。学年与发布日期都留空，
        系统只会把这份文件当成孤立的一份，不会和 2024 年版并列比较。
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

      <!-- R15：上传同主题/高度相似文件时的"是否政策更新"提示。
           只提示与一键关联，不自动建联、不自动改有效状态 -->
      <div v-if="uploadPrompt" class="update-prompt">
        <p class="prompt-head">
          <strong>这份文件可能属于政策更新</strong>
          <span class="muted tiny">——与下列已有文件主题相同或高度相似，请确认新旧关系</span>
        </p>
        <ul class="prompt-list">
          <li v-for="c in uploadPrompt.strong" :key="c.document_id" class="prompt-item">
            <label>
              <input v-model="linkForm.oldId" type="radio" :value="c.document_id" />
              <strong>{{ c.title }}</strong>
              <span class="tag plain">主题相同</span>
            </label>
            <span class="muted tiny">
              {{ c.published_at ?? '发布日期未知' }} · {{ EFFECTIVE_LABEL[c.effective_status] ?? c.effective_status }}
            </span>
          </li>
          <li v-for="c in uploadPrompt.weak" :key="c.document_id" class="prompt-item">
            <label>
              <input v-model="linkForm.oldId" type="radio" :value="c.document_id" />
              <strong>{{ c.title }}</strong>
              <span class="tag plain">标题高度相似</span>
            </label>
            <span class="muted tiny">
              {{ c.published_at ?? '发布日期未知' }} · {{ EFFECTIVE_LABEL[c.effective_status] ?? c.effective_status }}
            </span>
          </li>
        </ul>
        <div class="prompt-actions">
          <select v-model="linkForm.newStatus" class="meta-input">
            <option v-for="st in effectiveOptions" :key="st" :value="st">
              新版本标为「{{ EFFECTIVE_LABEL[st] }}」
            </option>
          </select>
          <button class="btn" :disabled="linking || !linkForm.oldId" @click="confirmUploadLink">
            {{ linking ? '处理中…' : '是，建立版本关联' }}
          </button>
          <button class="btn btn-ghost" :disabled="linking" @click="dismissUploadPrompt">
            否，保持独立
          </button>
          <span v-if="linkError" class="err-line">{{ linkError }}</span>
        </div>
        <p class="muted tiny">
          建立关联会把所选旧文件标记「已被替代」。不确定就先选「保持独立」，之后可在文件详情里关联。
        </p>
      </div>
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
            <th>版本</th>
            <th class="num">片段</th>
            <th>上传时间</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <template v-for="doc in docs" :key="doc.id">
            <tr
              :id="`doc-${doc.id}`"
              :class="{ active: openDocId === doc.id, highlight: highlightId === doc.id }"
            >
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
              <td>
                <div class="version-cell">
                  <span v-if="doc.academic_year || doc.published_at" class="muted tiny">
                    <template v-if="doc.academic_year">{{ doc.academic_year }}</template>
                    <template v-if="doc.academic_year && doc.published_at"> · </template>
                    <template v-if="doc.published_at">{{ doc.published_at.slice(0, 10) }}</template>
                  </span>
                  <!-- unknown 用中性灰：把它染成绿色会被读成"已经确认有效"，
                       而这一列的诚实含义是"上传时没标，系统不知道" -->
                  <span :class="['tag', doc.effective_status === 'active' ? 'met' : 'plain']">
                    {{ EFFECTIVE_LABEL[doc.effective_status] ?? doc.effective_status }}
                  </span>
                </div>
              </td>
              <td class="num">{{ doc.chunk_count }}</td>
              <td class="muted small">{{ formatTime(doc.created_at) }}</td>
              <td>
                <button
                  v-if="auth.isAdmin"
                  class="link"
                  @click="startEdit(doc)"
                >版本信息</button>
                <button
                  v-if="auth.isAdmin"
                  class="link danger"
                  @click="removeDoc(doc)"
                >下架</button>
                <FavoriteButton target-type="document" :target-id="doc.id" />
              </td>
            </tr>
            <tr v-if="editingId === doc.id" class="detail-row">
              <td colspan="8">
                <div class="edit-grid">
                  <label class="field">
                    <span>学年</span>
                    <input v-model="editForm.academic_year" placeholder="如 2025-2026" class="meta-input" />
                  </label>
                  <label class="field">
                    <span>发布日期</span>
                    <input v-model="editForm.published_at" type="date" class="meta-input" />
                  </label>
                  <label class="field">
                    <span>有效状态</span>
                    <select v-model="editForm.effective_status" class="meta-input">
                      <option value="">暂未确认（存入 unknown）</option>
                      <option v-for="s in effectiveOptions" :key="s" :value="s">
                        {{ EFFECTIVE_LABEL[s] }}
                      </option>
                    </select>
                  </label>
                </div>
                <p class="muted tiny">
                  只改这三项，不会重新解析文件、也不会动已有片段与已复核规则。
                </p>
                <p v-if="editError" class="err-line">{{ editError }}</p>
                <div class="edit-actions">
                  <button class="btn btn-sm" :disabled="editSaving" @click="saveEdit(doc)">
                    {{ editSaving ? '保存中…' : '保存' }}
                  </button>
                  <button class="btn btn-ghost btn-sm" :disabled="editSaving" @click="editingId = ''">
                    取消
                  </button>
                </div>
              </td>
            </tr>
            <tr v-if="openDocId === doc.id" class="detail-row">
              <td colspan="8">
                <!-- R15 旧版本提示：「已被替代」是管理员确认的事实，
                     「不是最新」只是日期推断，两句话不混用 -->
                <div
                  v-if="versionBanner(doc)"
                  :class="['version-banner', versionBanner(doc)!.tone]"
                >
                  <span>{{ versionBanner(doc)!.text }}</span>
                  <button
                    v-if="docVersions && docVersions.newest_document_id !== doc.id"
                    class="btn btn-ghost"
                    @click="openVersion(docVersions.versions.find(
                      (v) => v.document_id === docVersions!.newest_document_id) as VersionBrief)"
                  >
                    查看新版本
                  </button>
                </div>

                <!-- R17 收藏入口：文件详情页也能直接收藏/取消 -->
                <div class="detail-actions">
                  <FavoriteButton target-type="document" :target-id="doc.id" />
                  <span class="muted tiny">收藏后在「我的收藏」里随取随用。</span>
                </div>

                <!-- 基本信息（需求二.1 的八项） -->
                <dl class="meta-grid">
                  <div><dt>政策名称</dt><dd>{{ doc.title }}</dd></div>
                  <div><dt>发布部门</dt><dd>{{ doc.department ?? '未标注' }}</dd></div>
                  <div><dt>发布日期</dt><dd class="mono">{{ doc.published_at ?? '未标注' }}</dd></div>
                  <div><dt>适用学年</dt><dd class="mono">{{ doc.academic_year ?? '未标注' }}</dd></div>
                  <div><dt>文件版本</dt><dd>{{ doc.version_label ?? '未标注' }}</dd></div>
                  <div>
                    <dt>当前有效状态</dt>
                    <dd>
                      <span :class="['tag', doc.effective_status === 'active' ? 'met' : 'plain']">
                        {{ EFFECTIVE_LABEL[doc.effective_status] ?? doc.effective_status }}
                      </span>
                    </dd>
                  </div>
                  <div><dt>上传时间</dt><dd class="mono">{{ formatTime(doc.created_at) }}</dd></div>
                  <div>
                    <dt>关联历史版本</dt>
                    <dd>
                      <template v-if="linkedOldTitle(doc)">{{ linkedOldTitle(doc) }}</template>
                      <template v-else>未关联</template>
                    </dd>
                  </div>
                </dl>

                <!-- R16 纠错入口：学生指出这份政策哪里写错了（不改内容，只记反馈） -->
                <div class="correction-entry">
                  <button class="link" @click="correction = { open: true, doc }">
                    报告此处有误
                  </button>
                  <span class="muted tiny">——发现政策内容有误？提交后管理员会核对。</span>
                </div>

                <!-- 政策摘要：scope_note + 原文开头。不是生成的摘要，
                     摘要写错了会变成"看起来很权威的假话" -->
                <div class="summary-block">
                  <h4>政策摘要</h4>
                  <p v-if="doc.scope_note">{{ doc.scope_note }}</p>
                  <p v-if="chunks.length" class="muted">
                    原文开头：{{ chunks[0].content.slice(0, 160) }}{{ chunks[0].content.length > 160 ? '…' : '' }}
                  </p>
                  <p v-else-if="!chunkLoading" class="muted small">暂无正文可摘录。</p>
                </div>

                <!-- 历史版本：点击打开对应文件与原文 -->
                <div class="version-block">
                  <h4>
                    历史版本
                    <span v-if="docVersions" class="muted small">（同主题共 {{ docVersions.count }} 份）</span>
                  </h4>
                  <div v-if="versionLoading" class="muted small">版本信息加载中…</div>
                  <template v-else-if="docVersions && docVersions.count > 1">
                    <ul class="version-list">
                      <li
                        v-for="v in docVersions.versions"
                        :key="v.document_id"
                        :class="['version-item', { current: v.is_current }]"
                        role="button"
                        tabindex="0"
                        @click="openVersion(v)"
                        @keydown.enter="openVersion(v)"
                      >
                        <span class="tag plain">{{ EFFECTIVE_LABEL[v.effective_status] ?? v.effective_status }}</span>
                        <strong>{{ v.title }}</strong>
                        <span class="muted tiny mono">
                          {{ v.published_at ?? '发布日期未知' }}
                          <template v-if="v.version_label"> · {{ v.version_label }}</template>
                        </span>
                        <span v-if="v.is_current" class="tag met">当前查看</span>
                        <span class="open-hint">打开原文 →</span>
                      </li>
                    </ul>
                    <p class="muted tiny">{{ docVersions.hint }}</p>
                  </template>
                  <p v-else class="muted small">未发现同主题的其他版本。</p>
                </div>

                <!-- 管理员：手动建立版本关联（标题改过名的版本只能这样建） -->
                <div v-if="auth.isAdmin" class="link-block">
                  <h4>关联版本（管理员）</h4>
                  <div class="prompt-actions">
                    <select v-model="linkForm.oldId" class="meta-input">
                      <option value="">选择被替代的旧文件…</option>
                      <option
                        v-for="v in (docVersions?.versions ?? []).filter((x) => x.document_id !== doc.id)"
                        :key="v.document_id"
                        :value="v.document_id"
                      >
                        {{ v.title }}
                      </option>
                    </select>
                    <select v-model="linkForm.newStatus" class="meta-input">
                      <option v-for="st in effectiveOptions" :key="st" :value="st">
                        本文件标为「{{ EFFECTIVE_LABEL[st] }}」
                      </option>
                    </select>
                    <button class="btn" :disabled="linking || !linkForm.oldId" @click="linkVersion">
                      {{ linking ? '处理中…' : '建立关联' }}
                    </button>
                    <span v-if="linkMsg" class="ok-line">{{ linkMsg }}</span>
                    <span v-if="linkError" class="err-line">{{ linkError }}</span>
                  </div>
                  <p class="muted tiny">
                    确认后：所选旧文件标记「已被替代」，本文件标记为所选状态，并记录审计。
                  </p>
                </div>

                <!-- 相关文件：同一发布部门的其他文件（真实关联，不做语义推荐） -->
                <div v-if="relatedDocs.length" class="related-block">
                  <h4>相关文件（同发布部门）</h4>
                  <ul class="version-list">
                    <li
                      v-for="r in relatedDocs"
                      :key="r.id"
                      class="version-item"
                      role="button"
                      tabindex="0"
                      @click="openDocById(r.id)"
                      @keydown.enter="openDocById(r.id)"
                    >
                      <span class="tag plain">{{ EFFECTIVE_LABEL[r.effective_status] ?? r.effective_status }}</span>
                      <strong>{{ r.title }}</strong>
                      <span class="open-hint">打开 →</span>
                    </li>
                  </ul>
                </div>

                <div v-if="chunkLoading" class="muted small">片段加载中…</div>
                <div v-else-if="!chunks.length" class="muted small">该文件没有片段。</div>
                <div v-else class="chunk-list">
                  <h4>原文片段</h4>
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
            <td colspan="8" class="muted empty">暂无文件。管理员可在上方上传。</td>
          </tr>
        </tbody>
      </table>

      <div v-if="pages > 1" class="pager">
        <button class="btn btn-ghost" :disabled="page <= 1" @click="page--">上一页</button>
        <span class="muted small">{{ page }} / {{ pages }}</span>
        <button class="btn btn-ghost" :disabled="page >= pages" @click="page++">下一页</button>
      </div>
    </section>

    <!-- R16 纠错反馈弹层：政策详情 -->
    <CorrectionModal
      v-if="correction.doc"
      :open="correction.open"
      target-type="document"
      :target-id="correction.doc.id"
      :document-id="correction.doc.id"
      :target-title="correction.doc.title"
      @close="correction.open = false"
    />
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
  background: var(--c-surface-2);
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

.upload-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--space-3);
  margin-top: var(--space-3);
}

.upload-grid .field > span {
  font-size: 12px;
  color: var(--c-text-2);
}

.meta-input,
.select.meta-input {
  min-height: 38px;
  padding: 0 var(--space-3);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-control);
  background: var(--c-surface-2);
  font-family: inherit;
  font-size: 13px;
  color: var(--c-text);
}

.version-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  align-items: flex-start;
}

/* 操作列里的两个按钮是裸 <button class="link">，没有间距会粘成一个词 */
.table td .link + .link {
  margin-left: var(--space-3);
}

.edit-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--space-3);
  max-width: 720px;
  margin-bottom: var(--space-2);
}

.edit-grid .field > span {
  font-size: 12px;
  color: var(--c-text-2);
}

.edit-actions {
  display: flex;
  gap: var(--space-2);
  margin-top: var(--space-2);
}

.btn-sm {
  min-height: 32px;
  padding: 0 12px;
  font-size: 12px;
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

/* 从问答页带 ?doc= 跳进来时，这一行要一眼能认出来 */
.table tr.highlight td {
  background: var(--c-primary-weak);
  box-shadow: inset 3px 0 0 var(--c-primary);
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
  border-color: var(--c-met-border);
  background: var(--c-met-bg);
}

.tag.unknown {
  color: var(--c-unknown);
  border-color: var(--c-warn-border);
  background: var(--c-warn-bg);
}

.tag.not_met {
  color: var(--c-not-met);
  border-color: var(--c-not-met-border);
  background: var(--c-not-met-bg);
}

.tag.plain {
  color: var(--c-text-2);
  background: var(--c-bg);
}

.detail-row td {
  background: var(--c-surface-2);
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

/* ---------- R15：版本与更新提醒 ---------- */
.version-banner {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-control);
  margin-bottom: var(--space-3);
  font-size: 12.5px;
}
.version-banner.warn {
  background: var(--c-not-met-bg);
  border: 1px solid var(--c-not-met-border);
  color: var(--c-not-met);
  font-weight: 600;
}
.version-banner.info {
  background: var(--c-warn-bg);
  border: 1px solid var(--c-warn-border);
  color: var(--c-text);
}

.meta-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
  gap: var(--space-2) var(--space-3);
  margin: 0 0 var(--space-3);
}
.meta-grid dt {
  font-size: 11px;
  color: var(--c-text-2);
}
.meta-grid dd {
  margin: 2px 0 0;
  font-size: 12.5px;
  overflow-wrap: anywhere;
}

.correction-entry {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  margin: 0 0 var(--space-3);
}
.correction-entry .link {
  font-size: 12.5px;
}

.detail-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin: 0 0 var(--space-3);
}

.summary-block,
.version-block,
.link-block,
.related-block {
  margin: 0 0 var(--space-3);
  padding-top: var(--space-2);
  border-top: 1px dashed var(--c-border);
}
.summary-block h4,
.version-block h4,
.link-block h4,
.related-block h4 {
  margin: 0 0 var(--space-2);
  font-size: 12.5px;
}
.summary-block p {
  margin: 0 0 var(--space-1);
  font-size: 12.5px;
  line-height: 1.7;
}

.version-list {
  list-style: none;
  margin: 0 0 var(--space-2);
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.version-item {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  padding: 5px var(--space-2);
  border-radius: var(--radius-control);
  cursor: pointer;
  font-size: 12.5px;
}
.version-item:hover {
  background: var(--c-surface-2);
}
.version-item.current {
  border: 1px solid var(--c-primary);
}
.open-hint {
  margin-left: auto;
  font-size: 11px;
  color: var(--c-primary);
  opacity: 0;
  white-space: nowrap;
}
.version-item:hover .open-hint {
  opacity: 1;
}

.update-prompt {
  margin-top: var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--c-warn-border);
  background: var(--c-warn-bg);
  border-radius: var(--radius-control);
}
.prompt-head {
  margin: 0 0 var(--space-2);
  font-size: 12.5px;
}
.prompt-list {
  list-style: none;
  margin: 0 0 var(--space-2);
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.prompt-item {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  font-size: 12.5px;
}
.prompt-item label {
  display: flex;
  align-items: baseline;
  gap: 6px;
  cursor: pointer;
}
.prompt-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
</style>

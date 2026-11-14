import { defineStore } from 'pinia'
import type { MatchData } from '../api/client'

/**
 * 资格匹配的跨页留存（R8）。
 *
 * **它解决的是"切页丢画像"**：MatchView 卸载时 `documentId / profile / result`
 * 三个 ref 一起消失。用户填完一整屏（学生类型、年级、绩点、排名、困难认定、
 * 处分记录、专业、学分、学籍状态）——九项，然后点一下「智能问答」又点回来，
 * 全部变回"未填写"，连已经跑出来的匹配结论也没了。数据从来没离开过浏览器，
 * 丢的只是组件实例。
 *
 * 四条设计取舍：
 *
 * 1. **`sessionStorage` 而不是 `localStorage`**：画像里有绩点、专业排名、
 *    家庭经济困难认定——这些是敏感信息。`localStorage` 会在关掉浏览器后
 *    长期留在磁盘上，别人打开这台机器的同一个人浏览器就能看到。
 *    `sessionStorage` 关标签页即失效，和 `stores/qa.ts` 用同一把尺子。
 * 2. **不防抖，写即落盘**：数据量小（画像几百字节，一次匹配结论几 KB），
 *    没有"每追加一条就 stringify 整个会话"那种放大效应。
 *    而防抖引入的窗口期正好覆盖"用户输完最后一个字、立刻点侧栏切页"这个动作。
 * 3. **`result` 也存**：只存输入不存结论的话，用户回来看到的是
 *    "九项都填着、结论区空着"，还得再点一次「开始匹配」——
 *    可匹配是确定性的（不调模型），同样的输入必然同样的输出，
 *    让他再点一次只是重复劳动。结论本身带 `session_id`，
 *    「生成计划草稿」仍然能用。
 * 4. **`profile` 原地改，不整体替换**：`restore()` 用 `Object.assign`。
 *    组件里 `const profile = match.profile` 拿的是同一个响应式对象引用，
 *    整体替换会让它指向旧对象，于是"恢复成功但界面还是空的"。
 */

const KEY = 'campus_match_session'

/** 画像字段的初始值。键名与后端白名单一致，全部是可清空的字符串 */
export function emptyProfile(): Record<string, string> {
  return {
    student_type: '',
    grade: '',
    gpa: '',
    rank_percent: '',
    poverty_certified: '',
    disciplinary_record: '',
    major: '',
    credits_earned: '',
    enrollment_status: '',
  }
}

/**
 * 年级的合法上限由学生类型决定。
 *
 * **只对用户明确指定的两类设限**：专科 1–3、本科 1–4。
 * 其余类型（硕士/博士/非全日制/交换生/旁听生）仍保持 1–8——
 * 不是忘了，是这几类没有给出口径，凭猜写一个上限等于替用户定义学制。
 * 等口径明确再加一行即可。
 */
export const GRADE_MAX_BY_TYPE: Record<string, number> = {
  全日制在校专科生: 3,
  全日制在校本科生: 4,
}

/** 未选学生类型（或类型不在上表里）时的年级上限 */
export const GRADE_MAX_DEFAULT = 8
export const GRADE_MIN = 1

interface MatchState {
  documentId: string
  targetGroup: string
  profile: Record<string, string>
  result: MatchData | null
  confirmedId: string
}

/**
 * 把外部读回来的东西变干净再进 state。
 *
 * 缓存可以被手改、也可能是上个版本写的。`result` 直接塞给模板渲染
 * （`items[].status` 决定颜色分支、`summary` 决定三个计数），
 * 只校验"是不是对象"是不够的——结构不对会让整页白屏。
 */
function sanitizeResult(raw: unknown): MatchData | null {
  if (!raw || typeof raw !== 'object') return null
  const obj = raw as Partial<MatchData>
  if (!Array.isArray(obj.items) || !obj.summary || typeof obj.summary !== 'object') return null
  return obj as MatchData
}

export const useMatchStore = defineStore('match', {
  state: (): MatchState => ({
    documentId: '',
    targetGroup: '',
    profile: emptyProfile(),
    result: null,
    confirmedId: '',
  }),

  getters: {
    /** 当前学生类型对应的年级上限；未选类型时给默认值 8 */
    gradeMax: (state): number =>
      GRADE_MAX_BY_TYPE[state.profile.student_type] ?? GRADE_MAX_DEFAULT,
    /** 年级下拉的候选项：1..上限。用户也可以不选、直接输 */
    gradeOptions(): number[] {
      return Array.from({ length: this.gradeMax }, (_, i) => i + 1)
    },
  },

  actions: {
    persist() {
      try {
        sessionStorage.setItem(KEY, JSON.stringify({
          documentId: this.documentId,
          targetGroup: this.targetGroup,
          profile: this.profile,
          result: this.result,
          confirmedId: this.confirmedId,
        }))
      } catch {
        /* 配额满或 storage 被禁用：宁可这次不缓存，也不能让匹配流程崩掉 */
      }
    },

    /**
     * 从 sessionStorage 恢复。**坏缓存要自清**：否则每次进页面都解析失败，
     * 页面看起来就是"一打开资格匹配就崩"，而用户什么也没做错。
     */
    restore(): boolean {
      let raw: string | null = null
      try {
        raw = sessionStorage.getItem(KEY)
      } catch {
        return false
      }
      if (!raw) return false
      try {
        const parsed = JSON.parse(raw) as Partial<MatchState>
        const base = emptyProfile()
        if (parsed.profile && typeof parsed.profile === 'object') {
          // 只认已知键 + 只认字符串：多出来的键不进 state，
          // 因为 buildProfile() 只遍历 fields，多的键留着也发不出去，反而占地方
          for (const key of Object.keys(base)) {
            const value = (parsed.profile as Record<string, unknown>)[key]
            if (typeof value === 'string') base[key] = value
          }
        }
        this.documentId = typeof parsed.documentId === 'string' ? parsed.documentId : ''
        this.targetGroup = typeof parsed.targetGroup === 'string' ? parsed.targetGroup : ''
        /**
         * **原地改，不能写 `this.profile = base`。**
         *
         * MatchView 里 `const profile = match.profile` 取的是这个对象的引用
         * （模板里 `profile[f.key] = ...` 直接写 store）。整体替换会让它继续指向
         * 被换掉的那个旧对象：`restore()` 明明成功、界面还是一排"未填写"。
         * 这个坑在 R8 的浏览器验收里被抓到——**刷新整页时必现**，
         * 而"切页回来"因为上一轮 restore 已经把值留在旧对象里，看起来是好的。
         */
        Object.assign(this.profile, base)
        this.result = sanitizeResult(parsed.result)
        this.confirmedId = typeof parsed.confirmedId === 'string' ? parsed.confirmedId : ''
        return Boolean(this.documentId || this.result || Object.values(base).some(Boolean))
      } catch {
        try {
          sessionStorage.removeItem(KEY)
        } catch {
          /* 连删都删不掉（storage 被禁用）——那就当没有缓存 */
        }
        return false
      }
    },

    setResult(data: MatchData | null) {
      this.result = data
      if (!data) this.confirmedId = ''
    },

    /** 换政策文件时清结论。画像**不清**——同一份画像换份文件重比是常见用法 */
    resetResult() {
      this.result = null
      this.confirmedId = ''
    },

    /** 只清结论区不算"重置"，用户要的是把九项输入也清掉 */
    clearAll() {
      this.documentId = ''
      this.targetGroup = ''
      // 同上：原地清，保住 MatchView 手里的引用
      Object.assign(this.profile, emptyProfile())
      this.result = null
      this.confirmedId = ''
      try {
        sessionStorage.removeItem(KEY)
      } catch {
        /* 同上 */
      }
    },
  },
})

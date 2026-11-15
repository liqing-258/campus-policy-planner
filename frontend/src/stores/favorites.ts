import { defineStore } from 'pinia'
import { api, type FavoriteTarget } from '../api/client'

/**
 * 常用政策收藏（R17）。
 *
 * 收藏状态落在数据库（favorites 表），这个 store 只是前端对「当前用户收藏了哪些
 * 目标」的内存镜像——用来给各处的收藏按钮渲染「已收藏 / 未收藏」，以及「我的收藏」
 * 页面拉全量列表。
 *
 * 镜像的职责很窄：`keys` 是一组 `"target_type:target_id"` 字符串。真正的列表内容
 * （名称、有效状态、问答正文）走 `GET /favorites` 现取，不在 store 里缓存一份副本——
 * 缓存副本会与后端真值分叉，而「政策有效状态同步」要求每一次展示都现算。
 */

interface FavoriteState {
  /** `target_type:target_id` 的集合，代表当前用户已收藏的目标 */
  keys: Set<string>
  loaded: boolean
}

/** 模块级 in-flight 防重：多个收藏按钮同时挂载时只发一次 ids 请求 */
let inflight: Promise<void> | null = null

export const useFavoriteStore = defineStore('favorites', {
  state: (): FavoriteState => ({
    keys: new Set<string>(),
    loaded: false,
  }),

  getters: {
    /** 该目标是否已收藏。`type` 与后端 target_type 对齐（document/guide/qa/plan）。 */
    isFav: (state) => (type: FavoriteTarget, id: string) =>
      state.keys.has(`${type}:${id}`),
  },

  actions: {
    /** 拉一次当前用户已收藏的目标标识。切换账号后必须重新拉（标识按账号隔离）。 */
    async load() {
      if (this.loaded) return
      if (inflight) return inflight
      inflight = (async () => {
        const data = await api.get<{ ids: string[]; count: number }>('/favorites/ids')
        this.keys = new Set(data.ids)
        this.loaded = true
      })().finally(() => {
        inflight = null
      })
      return inflight
    },

    /**
     * 收藏 / 取消收藏，返回操作后的新状态（true=已收藏，false=已取消）。
     * 乐观更新镜像后立刻落库；失败时回滚镜像，避免按钮状态与后端不一致。
     */
    async toggle(type: FavoriteTarget, id: string): Promise<boolean> {
      const key = `${type}:${id}`
      const nowFav = this.keys.has(key)
      if (nowFav) {
        // 取消：先删镜像，再请求后端；失败则补回
        this.keys.delete(key)
        try {
          await api.remove(`/favorites/${type}/${id}`)
          return false
        } catch {
          this.keys.add(key)
          throw new Error('取消收藏失败')
        }
      }
      // 收藏：先加镜像，再请求后端；失败则移除
      this.keys.add(key)
      try {
        await api.post('/favorites', { target_type: type, target_id: id })
        return true
      } catch {
        this.keys.delete(key)
        throw new Error('收藏失败')
      }
    },

    /** 登出时清空镜像：收藏是账号级数据，不随会话缓存残留。 */
    reset() {
      this.keys = new Set()
      this.loaded = false
    },
  },
})

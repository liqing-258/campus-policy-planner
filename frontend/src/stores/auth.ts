import { defineStore } from 'pinia'
import { api, getToken, setToken, type LoginResult, type User } from '../api/client'

interface AuthState {
  token: string | null
  user: User | null
  loading: boolean
  error: string
}

export const useAuthStore = defineStore('auth', {
  state: (): AuthState => ({
    token: getToken(),
    user: null,
    loading: false,
    error: '',
  }),

  getters: {
    isLoggedIn: (state) => Boolean(state.token),
    isAdmin: (state) => state.user?.role === 'admin',
    displayName: (state) => state.user?.display_name ?? '',
  },

  actions: {
    async login(username: string, password: string) {
      this.loading = true
      this.error = ''
      try {
        const result = await api.post<LoginResult>('/auth/login', { username, password })
        this.token = result.token
        this.user = result.user
        setToken(result.token)
        return true
      } catch (err) {
        this.error = err instanceof Error ? err.message : '登录失败'
        return false
      } finally {
        this.loading = false
      }
    },

    /** 刷新页面后凭本地令牌恢复用户信息；失败即视为登出 */
    async restore() {
      if (!this.token) return false
      try {
        this.user = await api.get<User>('/auth/me')
        return true
      } catch {
        this.logout()
        return false
      }
    },

    logout() {
      this.token = null
      this.user = null
      setToken(null)
    },
  },
})

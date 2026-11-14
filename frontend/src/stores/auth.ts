import { defineStore } from 'pinia'
import {
  api,
  getToken,
  setToken,
  type LoginResult,
  type RegisterPayload,
  type RegisterResult,
  type User,
} from '../api/client'
import { useMatchStore } from './match'
import { useQAStore } from './qa'

/**
 * 用户信息也写进 localStorage（`zxx_user`）。
 *
 * 只存 token 是不够的：路由守卫在首次导航时就会跑，而 `restore()` 要等 App 挂载后
 * 才异步完成。守卫这期间读到 user=null，会把管理员也当成非管理员踢回工作台。
 * 存一份 user 让守卫有据可依；真权限仍由后端 403 决定，前端这一层只影响体验。
 */
const USER_KEY = 'zxx_user'

function readCachedUser(): User | null {
  try {
    const raw = localStorage.getItem(USER_KEY)
    return raw ? (JSON.parse(raw) as User) : null
  } catch {
    return null
  }
}

function cacheUser(user: User | null): void {
  if (user) localStorage.setItem(USER_KEY, JSON.stringify(user))
  else localStorage.removeItem(USER_KEY)
}

interface AuthState {
  token: string | null
  user: User | null
  loading: boolean
  error: string
}

export const useAuthStore = defineStore('auth', {
  state: (): AuthState => ({
    token: getToken(),
    user: readCachedUser(),
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
        cacheUser(result.user)
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
        cacheUser(this.user)
        return true
      } catch {
        this.logout()
        return false
      }
    },

    /**
     * 学生自助注册。
     *
     * 成功后**直接进入登录态**（后端把 token 一并返回了）：注册完还要再手打一遍
     * 刚填过的学号与密码，是纯粹的重复劳动。
     */
    async register(payload: RegisterPayload): Promise<RegisterResult | null> {
      this.loading = true
      this.error = ''
      try {
        const result = await api.post<RegisterResult>('/auth/register', payload)
        this.token = result.token
        this.user = result.user
        setToken(result.token)
        cacheUser(result.user)
        return result
      } catch (err) {
        this.error = err instanceof Error ? err.message : '注册失败'
        return null
      } finally {
        this.loading = false
      }
    },

    /**
     * 登出：清令牌、清用户，**并清掉问答与匹配两份会话缓存**。
     *
     * 两份缓存都落在 sessionStorage，不随登录态失效。不清的话，同一台机器换个人登录，
     * 一进问答页就能看到上一个人的提问与解答、一进匹配页就能看到上一个人的绩点与
     * 家庭经济困难认定——这是隐私泄漏，不只是体验问题。
     *
     * R9 补上 match：R8 把画像搬进 sessionStorage 时漏了这一处，而 qa 那份是早就有清过的。
     * 放在 store 里而不是按钮里，是因为 `restore()` 失败时也会走到这里。
     */
    logout() {
      useQAStore().clear()
      useMatchStore().clearAll()
      this.token = null
      this.user = null
      setToken(null)
      cacheUser(null)
    },
  },
})

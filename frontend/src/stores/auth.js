/**
 * 认证 store（W4 设计 §4.3）：
 * - token 对（access/refresh）+ user{id, username, hasKey}，localStorage 持久化
 * - 签发时解码 exp 存本地；ensureFreshToken：剩 <5min 先刷新再放行对话页（SSE 建流前保证 token 新鲜）
 * - ensureFresh：单飞刷新（并发 401 只发一次 /api/auth/refresh，防 refresh 旋转竞态）
 */
import { defineStore } from 'pinia'
import axios from 'axios'
import { toBizError } from '../api/errors'
import { decodeJwtExp } from '../utils/jwt'
import { storage } from '../utils/storage'

const K_ACCESS = 't2s_access'
const K_REFRESH = 't2s_refresh'
const K_USER = 't2s_user'
const K_EXP = 't2s_exp'

const REFRESH_AHEAD_MS = 5 * 60 * 1000 // 剩余有效期低于 5 分钟即预刷新

let refreshingPromise = null // 模块级单飞锁

function readJSON(key) {
  const raw = storage.get(key)
  if (!raw) return null
  try {
    return JSON.parse(raw)
  } catch {
    return null
  }
}

export const useAuthStore = defineStore('auth', {
  state: () => ({
    accessToken: storage.get(K_ACCESS) || '',
    refreshToken: storage.get(K_REFRESH) || '',
    user: readJSON(K_USER), // {id, username, hasKey}
    expAt: Number(storage.get(K_EXP)) || 0, // access token 过期时刻（ms）
  }),

  getters: {
    isLoggedIn: (s) => !!s.accessToken,
    hasKey: (s) => !!s.user?.hasKey,
  },

  actions: {
    /** 登录/注册/刷新成功后统一入口：data = {accessToken, refreshToken, user} */
    setSession(data) {
      this.accessToken = data.accessToken
      this.refreshToken = data.refreshToken
      this.user = data.user || null
      this.expAt = decodeJwtExp(data.accessToken) || 0
      storage.set(K_ACCESS, this.accessToken)
      storage.set(K_REFRESH, this.refreshToken)
      storage.set(K_USER, JSON.stringify(this.user))
      storage.set(K_EXP, String(this.expAt))
    },

    /** hasKey 变化时单独同步（如设置页录/删 key 后） */
    setUserHasKey(hasKey) {
      if (this.user) {
        this.user = { ...this.user, hasKey }
        storage.set(K_USER, JSON.stringify(this.user))
      }
    },

    clear() {
      this.accessToken = ''
      this.refreshToken = ''
      this.user = null
      this.expAt = 0
      refreshingPromise = null
      ;[K_ACCESS, K_REFRESH, K_USER, K_EXP].forEach((k) => storage.remove(k))
    },

    /** 用 refreshToken 换发新双 token（后端旋转）。失败清会话并抛 BizError。 */
    async refresh() {
      if (!this.refreshToken) {
        this.clear()
        throw toBizError('invalid_refresh', 401)
      }
      try {
        // 裸 axios（不带实例拦截器，避免递归）
        const res = await axios.post('/api/auth/refresh', { refreshToken: this.refreshToken })
        const body = res.data
        if (!body || body.code !== 'ok') {
          throw toBizError(body?.code || 'invalid_refresh', res.status, body?.message)
        }
        this.setSession(body.data)
        return body.data
      } catch (e) {
        this.clear()
        throw e.name === 'BizError' ? e : toBizError('invalid_refresh', 401)
      }
    },

    /** 单飞刷新：并发调用共享同一次请求。 */
    ensureFresh() {
      if (!refreshingPromise) {
        refreshingPromise = this.refresh().finally(() => {
          refreshingPromise = null
        })
      }
      return refreshingPromise
    },

    /**
     * 本地 exp 预检（§4.3，路由守卫与开流前调用）：
     * 剩余有效期 < 5min（或 exp 不可解析）→ 先刷新再放行。
     */
    async ensureFreshToken() {
      if (!this.accessToken) throw toBizError('auth_required', 401)
      const remain = this.expAt ? this.expAt - Date.now() : null
      if (remain === null || remain < REFRESH_AHEAD_MS) {
        await this.ensureFresh()
      }
    },
  },
})

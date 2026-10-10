/**
 * axios 实例（W4 设计 §4.1）：
 * - 请求拦截：附 Authorization
 * - 响应拦截：
 *   · 统一信封 {code, message, data}，code !== 'ok' → 抛 BizError（文案出自 errors.js §3.3 表）
 *   · HTTP 401 → 用 refreshToken 静默调 /api/auth/refresh（单飞，防并发竞态）→ 重放原请求一次
 *   · refresh 也失败 → 清 store、跳登录、抛 invalid_refresh
 */
import axios from 'axios'
import { toBizError } from './errors'
import { useAuthStore } from '../stores/auth'
import router from '../router'

const api = axios.create({
  baseURL: '/api',
  timeout: 20000,
})

api.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.accessToken) config.headers.Authorization = `Bearer ${auth.accessToken}`
  return config
})

api.interceptors.response.use(
  (res) => {
    const body = res.data
    if (body && typeof body === 'object' && 'code' in body && body.code !== 'ok') {
      // 200 信封里携带业务错误：同样按 §3.3 抛给调用方
      throw toBizError(body.code, res.status, body.message)
    }
    return body // 调用方直接拿 {code, message, data}
  },
  async (err) => {
    const { response, config } = err
    if (response?.status === 401 && config && !config._retried) {
      const auth = useAuthStore()
      try {
        await auth.ensureFresh() // 单飞刷新（可能已被别的并发 401 触发）
        config._retried = true // 只重放一次
        return api(config)
      } catch {
        auth.clear()
        router.push('/login')
        throw toBizError('invalid_refresh', 401)
      }
    }
    const code = response?.data?.code
    throw toBizError(code, response?.status ?? 0, response?.data?.message)
  }
)

export default api

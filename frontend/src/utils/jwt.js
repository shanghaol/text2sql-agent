/**
 * 解析 JWT payload 中的 exp（毫秒时间戳）。纯函数，供 auth store 做本地过期预检（§4.3）。
 * 只解码不验签——前端预检仅为体验优化，权威校验在 Java 侧。
 */
export function decodeJwtExp(token) {
  if (!token || typeof token !== 'string') return null
  const parts = token.split('.')
  if (parts.length < 2) return null
  try {
    const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    const json = decodeURIComponent(
      atob(b64)
        .split('')
        .map((c) => '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2))
        .join('')
    )
    const payload = JSON.parse(json)
    return typeof payload.exp === 'number' ? payload.exp * 1000 : null
  } catch {
    return null
  }
}

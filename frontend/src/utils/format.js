/** 生成 RFC4122 v4 uuid（threadId 用）；无 crypto.randomUUID 的环境退化为手工拼。 */
export function uuid() {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID()
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    const v = c === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}

/** 后端 LocalDateTime（ISO 字符串）→ 本地可读时间；解析失败原样返回。 */
export function fmtDateTime(s) {
  if (!s) return '-'
  const d = new Date(s)
  if (isNaN(d.getTime())) return String(s)
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

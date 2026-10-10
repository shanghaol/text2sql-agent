/** localStorage 安全封装：不可用（隐私模式/测试环境）时退化为内存 Map。 */
const mem = new Map()
const ls = typeof localStorage !== 'undefined' ? localStorage : null

export const storage = {
  get(key) {
    try {
      return ls ? ls.getItem(key) : mem.get(key) ?? null
    } catch {
      return mem.get(key) ?? null
    }
  },
  set(key, value) {
    try {
      if (ls) ls.setItem(key, value)
      else mem.set(key, value)
    } catch {
      mem.set(key, value)
    }
  },
  remove(key) {
    try {
      if (ls) ls.removeItem(key)
      else mem.delete(key)
    } catch {
      mem.delete(key)
    }
  },
}

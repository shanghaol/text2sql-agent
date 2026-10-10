/**
 * 错误码 → 用户文案映射（W4 设计 §3.3 汇总表 + §1.2 BYOK 校验文案表）。
 * 前端唯一参照：所有 BizError 的 message 都从这里出，不散落在调用点。
 */
export const ERROR_MESSAGES = {
  // 认证（Java）
  auth_required: '登录状态已过期，请重新登录',
  invalid_refresh: '登录已失效，请重新登录',
  bad_credentials: '用户名或密码错误',
  username_taken: '用户名已被占用，请换一个',
  // 网关（Java）
  key_not_set: '尚未配置 DeepSeek Key，请先到设置页录入',
  rate_limited: '请求过于频繁，请稍后再试',
  concurrency_full: '当前使用人数较多，请稍后再试',
  // BYOK 校验（Java，§1.2 文案表逐条对应）
  key_invalid: 'Key 无效或已被删除，请到 DeepSeek 平台核对',
  verify_rate_limited: '校验请求过于频繁，请一分钟后再试',
  verify_unreachable: 'DeepSeek 校验服务暂时不可达，请稍后重试（本次未保存）',
  bad_key_format: 'Key 格式不正确，应以 sk- 开头',
  // 业务
  history_not_found: '该记录不存在或已被删除',
  // 流中错误（Python，§1.6 表）
  llm_key_missing: '服务端缺少 LLM Key，请联系管理员',
  llm_key_invalid: 'Key 已失效，请到设置页重新录入',
  llm_insufficient_balance: 'DeepSeek 余额不足，请充值后重试',
  llm_rate_limited: '请求过于频繁，请稍后再试',
  llm_upstream_error: '模型服务暂时不可用，请重试',
  // 兜底
  bad_request: '请求参数不合法',
  forbidden: '无权执行该操作',
  internal_error: '服务内部错误，请稍后重试',
  network_error: '网络异常，请检查连接后重试',
}

export function messageFor(code, fallback) {
  if (code && ERROR_MESSAGES[code]) return ERROR_MESSAGES[code]
  if (fallback) return fallback
  return code ? `未知错误（${code}）` : ERROR_MESSAGES.internal_error
}

/** 业务错误：携带 code（§3.3 错误码）与 httpStatus，message 已映射为可读文案。 */
export class BizError extends Error {
  constructor(code, httpStatus = 0, rawMessage = null) {
    super(messageFor(code, rawMessage))
    this.name = 'BizError'
    this.code = code || 'internal_error'
    this.httpStatus = httpStatus
  }
}

export function toBizError(code, httpStatus, rawMessage) {
  return new BizError(code, httpStatus, rawMessage)
}

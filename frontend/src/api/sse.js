/**
 * SSE 消费：fetch + ReadableStream 手写解析（W4 设计 §4.2，否决 EventSource）。
 * EventSource 两个硬伤：只支持 GET（对话要 POST JSON body）；不能带 Authorization 头。
 *
 * §4.2 五要点逐条落点：
 * 1. res.ok === false → 读 JSON body 抛业务错误，不进流解析        → chatSSE()
 * 2. TextDecoder 流式 decode(stream:true)，按 \n\n 分帧，缓冲区保留不完整帧 → createSSEParser()
 * 3. 每帧解析 event:/data: 行（容忍 CRLF）；`:` 开头为注释行忽略    → createSSEParser()
 * 4. AbortController abort → fetch 抛 AbortError → 原样上抛，调用方标"已停止" → chatSSE()
 * 5. 网络中断（无 abort）→ 抛 network_error 业务错误，调用方保留已收文本
 */
import { toBizError } from './errors.js' // 显式扩展名：保证 node 脚本可直接 import 本模块联调

/**
 * 纯函数解析器（可单测）：喂任意切分的文本 chunk，吐完整事件数组。
 * chunk 边界不保证对齐事件边界——不完整帧留在内部缓冲区等下一个 chunk。
 */
export function createSSEParser() {
  let buf = ''

  function parseFrame(frame) {
    let event = 'message' // SSE 规范默认事件名（本契约总是显式携带 event:）
    const dataLines = []
    for (const raw of frame.split('\n')) {
      const line = raw.endsWith('\r') ? raw.slice(0, -1) : raw // 容忍 CRLF
      if (line === '') continue
      if (line.startsWith(':')) continue // 注释行（心跳占位，当前后端不发）
      const colon = line.indexOf(':')
      const field = colon === -1 ? line : line.slice(0, colon)
      let value = colon === -1 ? '' : line.slice(colon + 1)
      if (value.startsWith(' ')) value = value.slice(1) // 规范：去掉冒号后单个空格
      if (field === 'event') event = value
      else if (field === 'data') dataLines.push(value)
      // id:/retry: 本契约不用，忽略
    }
    if (dataLines.length === 0) return null // 无 data 的事件按规范不派发
    return { event, data: dataLines.join('\n') } // 多个 data: 行按规范以 \n 连接
  }

  return {
    /** 追加一段文本，返回本次凑齐的完整事件列表。 */
    push(chunkText) {
      buf += chunkText
      // CRLF 规范化：\r\n\r\n 分隔在 \n\n 切分下不可见；行尾残留的孤立 \r 留在缓冲区，
      // 与下一个 chunk 开头的 \n 拼上后同样被规范化（chunk 边界安全）
      if (buf.includes('\r')) buf = buf.replace(/\r\n/g, '\n')
      const events = []
      let idx
      while ((idx = buf.indexOf('\n\n')) !== -1) {
        const frame = buf.slice(0, idx)
        buf = buf.slice(idx + 2)
        const parsed = parseFrame(frame)
        if (parsed) events.push(parsed)
      }
      return events
    },
    /** 仅测试/调试用：查看缓冲区中尚未成帧的残留。 */
    get pending() {
      return buf
    },
  }
}

/** 契约适配：本服务所有事件的 data 均为 JSON；解析失败保留原始字符串兜底。 */
function adaptEvent({ event, data }) {
  try {
    return { event, data: JSON.parse(data) }
  } catch {
    return { event, data }
  }
}

/**
 * 对话 SSE 主入口：POST /api/chat（带 Authorization），逐事件回调 onEvent({event, data:object})。
 * - 流前错误（非 2xx）：读 JSON body 抛 BizError（key_not_set/rate_limited/concurrency_full/...）
 * - 用户停止：AbortError 原样上抛，由调用方标记"已停止"
 * - 网络中断：抛 BizError('network_error')，已收文本由调用方保留
 * - baseUrl 供 node 脚本直连后端联调使用，浏览器内为默认同源 ''
 * - path/body：W5 §3.7——审批恢复流复用同一解析器：path='/api/chat/approval' +
 *   body={threadId, approvalId, decision, historyId}；缺省时按 /api/chat 的
 *   {question, threadId} 组包（解析器与事件适配零变化）
 */
export async function chatSSE({ question, threadId, token, onEvent, signal, baseUrl = '', path = '/api/chat', body = null }) {
  let res
  try {
    res = await fetch(`${baseUrl}${path}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(body ?? { question, threadId }),
      signal,
    })
  } catch (e) {
    if (e && e.name === 'AbortError') throw e // 要点 4：停止 → 上抛
    throw toBizError('network_error', 0)
  }

  // 要点 1：流前错误按 JSON 报错，绝不进流解析
  if (!res.ok) {
    let code = 'internal_error'
    let raw = null
    try {
      const body = await res.json()
      code = body.code || code
      raw = body.message || null
    } catch {
      /* 非 JSON 错误体（如网关默认页），保留兜底码 */
    }
    throw toBizError(code, res.status, raw)
  }

  const parser = createSSEParser()
  const decoder = new TextDecoder()
  try {
    const reader = res.body.getReader() // 标准 Streams API：body 本身没有 read()
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      // 要点 2：stream:true 处理多字节字符跨 chunk 的场景
      for (const raw of parser.push(decoder.decode(value, { stream: true }))) onEvent(adaptEvent(raw))
    }
    const tail = decoder.decode() // 冲刷 decoder 残留字节
    if (tail) for (const raw of parser.push(tail)) onEvent(adaptEvent(raw))
  } catch (e) {
    if (e && e.name === 'AbortError') throw e
    // 要点 5：流中网络中断，已收文本由调用方保留，标记"连接中断，可重试"
    throw toBizError('network_error', 0)
  }
}

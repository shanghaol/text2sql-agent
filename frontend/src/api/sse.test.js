import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createSSEParser, chatSSE } from './sse'
import { BizError } from './errors'

const enc = new TextEncoder()

describe('createSSEParser §4.2 分帧解析', () => {
  it('解析完整单帧 event+data', () => {
    const p = createSSEParser()
    expect(p.push('event: meta\ndata: {"threadKey":"u42:abc","model":"deepseek-flash"}\n\n')).toEqual([
      { event: 'meta', data: '{"threadKey":"u42:abc","model":"deepseek-flash"}' },
    ])
  })

  it('chunk 边界不对齐时缓冲区保留不完整帧，跨 push 拼出完整事件', () => {
    const p = createSSEParser()
    // 半截帧：不产出任何事件
    expect(p.push('event: del')).toEqual([])
    expect(p.push('ta\ndata: {"text":"你')).toEqual([])
    expect(p.pending).not.toBe('')
    // 补齐后恰好解析一次，不丢不重
    expect(p.push('好"}\n\n')).toEqual([{ event: 'delta', data: '{"text":"你好"}' }])
    expect(p.pending).toBe('')
  })

  it('一次 push 多帧全部解析', () => {
    const p = createSSEParser()
    const events = p.push(
      'event: delta\ndata: {"text":"a"}\n\nevent: tool\ndata: {"name":"execute_readonly_sql"}\n\nevent: done\ndata: {"latencyMs":100}\n\n'
    )
    expect(events.map((e) => e.event)).toEqual(['delta', 'tool', 'done'])
  })

  it('容忍 CRLF 行尾', () => {
    const p = createSSEParser()
    expect(p.push('event: delta\r\ndata: {"text":"hi"}\r\n\r\n')).toEqual([
      { event: 'delta', data: '{"text":"hi"}' },
    ])
  })

  it('`: ` 开头注释行（心跳占位）被忽略', () => {
    const p = createSSEParser()
    const events = p.push(': heartbeat\n\nevent: done\ndata: {}\n\n')
    expect(events).toEqual([{ event: 'done', data: '{}' }])
  })

  it('无 data 的事件不派发；多 data: 行按规范以 \\n 连接', () => {
    const p = createSSEParser()
    expect(p.push('event: ping\n\n')).toEqual([])
    expect(p.push('event: delta\ndata: line1\ndata: line2\n\n')).toEqual([
      { event: 'delta', data: 'line1\nline2' },
    ])
  })

  it('流结束时残留不成帧的内容留在缓冲区（按 SSE 规范丢弃，不误派发）', () => {
    const p = createSSEParser()
    p.push('event: saved\ndata: {"historyId":1}\n\n')
    p.push('event: delta\ndata: {"text":"半截')
    expect(p.pending).toBe('event: delta\ndata: {"text":"半截')
  })
})

function sseResponse(chunks) {
  const stream = new ReadableStream({
    start(c) {
      for (const chunk of chunks) c.enqueue(enc.encode(chunk))
      c.close()
    },
  })
  return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

describe('chatSSE §4.2 端到端（stub fetch）', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('流前非 2xx：读 JSON body 抛业务错误，不进流解析（要点 1）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify({ code: 'key_not_set', message: 'no key' }), {
          status: 403,
          headers: { 'Content-Type': 'application/json' },
        })
      )
    )
    const onEvent = vi.fn()
    await expect(chatSSE({ question: 'q', threadId: 't', token: 'x', onEvent })).rejects.toMatchObject({
      code: 'key_not_set',
      httpStatus: 403,
    })
    expect(onEvent).not.toHaveBeenCalled()
  })

  it('正常流：事件按序回调（delta → tool → saved）', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => sseResponse([
      'event: meta\ndata: {"model":"deepseek-flash"}\n\n',
      'event: delta\ndata: {"text":"根"}\n\n',
      'event: tool\ndata: {"name":"execute_readonly_sql","sql":"SELECT 1"}\n\n',
      'event: saved\ndata: {"historyId":9}\n\n',
    ])))
    const events = []
    await chatSSE({ question: 'q', threadId: 't', token: 'x', onEvent: (e) => events.push(e) })
    expect(events.map((e) => e.event)).toEqual(['meta', 'delta', 'tool', 'saved'])
    expect(events[3].data).toEqual({ historyId: 9 })
  })

  it('AbortError 原样上抛（要点 4），不被包装成业务错误', async () => {
    const controller = new AbortController()
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url, init) => {
        // 模拟 fetch 尚未返回响应时被 abort
        return new Promise((_resolve, reject) => {
          init.signal.addEventListener('abort', () => {
            const e = new Error('The operation was aborted')
            e.name = 'AbortError'
            reject(e)
          })
        })
      })
    )
    const p = chatSSE({ question: 'q', threadId: 't', token: 'x', onEvent: () => {}, signal: controller.signal })
    controller.abort()
    await expect(p).rejects.toMatchObject({ name: 'AbortError' })
  })

  it('流中网络中断 → network_error 业务错误（要点 5）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        const stream = new ReadableStream({
          start(c) {
            c.enqueue(enc.encode('event: delta\ndata: {"text":"已收到部分"}\n\n'))
            c.error(new TypeError('network dropped'))
          },
        })
        return new Response(stream, { status: 200 })
      })
    )
    await expect(chatSSE({ question: 'q', threadId: 't', token: 'x', onEvent: () => {} })).rejects.toBeInstanceOf(
      BizError
    )
  })
})

import { describe, it, expect } from 'vitest'
import { applyChatEvent, newAssistantMsg } from './chat'

const ev = (event, data) => ({ event, data })

describe('applyChatEvent：SSE 事件 → 消息状态（§3.2 契约）', () => {
  it('delta 累加文本', () => {
    let m = newAssistantMsg()
    m = applyChatEvent(m, ev('delta', { text: '根据订单表' }))
    m = applyChatEvent(m, ev('delta', { text: '统计…' }))
    expect(m.text).toBe('根据订单表统计…')
    expect(m.streaming).toBe(true)
  })

  it('tool 起始事件：渲染指示行且同名去重', () => {
    let m = newAssistantMsg()
    m = applyChatEvent(m, ev('tool', { name: 'execute_readonly_sql' }))
    m = applyChatEvent(m, ev('tool', { name: 'execute_readonly_sql' })) // 后端同轮只发一次，防御性再压一次
    expect(m.tools).toEqual([{ name: 'execute_readonly_sql', sql: null }])
  })

  it('带 sql 的完成 tool 事件：更新同名指示行为"已执行 SQL"（mapper 累积）', () => {
    let m = newAssistantMsg()
    m = applyChatEvent(m, ev('tool', { name: 'execute_readonly_sql' }))
    m = applyChatEvent(m, ev('delta', { text: '查询中' }))
    m = applyChatEvent(m, ev('tool', { name: 'execute_readonly_sql', sql: 'SELECT region, SUM(gmv) FROM mart.orders GROUP BY region' }))
    expect(m.tools).toHaveLength(1)
    expect(m.tools[0]).toEqual({
      name: 'execute_readonly_sql',
      sql: 'SELECT region, SUM(gmv) FROM mart.orders GROUP BY region',
    })
  })

  it('多轮工具 + 多种工具：各自成行、完成事件对号入座', () => {
    let m = newAssistantMsg()
    m = applyChatEvent(m, ev('tool', { name: 'search_query_history' }))
    m = applyChatEvent(m, ev('tool', { name: 'search_query_history' })) // 无 sql 的重复起始：去重为一条
    m = applyChatEvent(m, ev('tool', { name: 'execute_readonly_sql' }))
    m = applyChatEvent(m, ev('tool', { name: 'execute_readonly_sql', sql: 'SELECT 1' }))
    m = applyChatEvent(m, ev('tool', { name: 'execute_readonly_sql', sql: 'SELECT 2 LIMIT 20' })) // 第二次执行 → 更新到最后一条同名
    expect(m.tools).toEqual([
      { name: 'search_query_history', sql: null },
      { name: 'execute_readonly_sql', sql: 'SELECT 2 LIMIT 20' },
    ])
  })

  it('done：收尾并记录 tokens/latency/generatedSql', () => {
    let m = newAssistantMsg()
    m = applyChatEvent(m, ev('done', { tokens: { input: 8123, output: 640 }, latencyMs: 7400, generatedSql: 'SELECT 1' }))
    expect(m.streaming).toBe(false)
    expect(m.tokens).toEqual({ input: 8123, output: 640 })
    expect(m.latencyMs).toBe(7400)
    expect(m.generatedSql).toBe('SELECT 1')
  })

  it('error：流中失败一次，映射可读文案', () => {
    let m = newAssistantMsg()
    m = applyChatEvent(m, ev('delta', { text: '部分回答' }))
    m = applyChatEvent(m, ev('error', { code: 'llm_insufficient_balance', message: 'DeepSeek 余额不足' }))
    expect(m.streaming).toBe(false)
    expect(m.errorCode).toBe('llm_insufficient_balance')
    expect(m.errorMessage).toBe('DeepSeek 余额不足，请充值后重试')
    expect(m.text).toBe('部分回答') // 已收文本不清除
  })

  it('saved：Java 注入 historyId，反馈按钮解锁依据', () => {
    let m = newAssistantMsg()
    m = applyChatEvent(m, ev('saved', { historyId: 123 }))
    expect(m.historyId).toBe(123)
  })

  it('全流程串跑：meta → tool(起) → delta×n → tool(完成带sql) → done → saved', () => {
    let m = newAssistantMsg()
    for (const e of [
      ev('meta', { threadKey: 'u42:t1', model: 'deepseek-flash' }),
      ev('tool', { name: 'execute_readonly_sql' }),
      ev('delta', { text: '上月 GMV 为' }),
      ev('delta', { text: ' 12345 元。' }),
      ev('tool', { name: 'execute_readonly_sql', sql: "SELECT SUM(amount) FROM orders WHERE dt >= '2026-09-01'" }),
      ev('done', { tokens: { input: 100, output: 20 }, latencyMs: 3000 }),
      ev('saved', { historyId: 7 }),
    ]) {
      m = applyChatEvent(m, e)
    }
    expect(m.model).toBe('deepseek-flash')
    expect(m.text).toBe('上月 GMV 为 12345 元。')
    expect(m.tools).toEqual([
      { name: 'execute_readonly_sql', sql: "SELECT SUM(amount) FROM orders WHERE dt >= '2026-09-01'" },
    ])
    expect(m.streaming).toBe(false)
    expect(m.historyId).toBe(7)
  })

  it('未知事件不崩、纯函数不改入参', () => {
    const before = newAssistantMsg()
    const snapshot = JSON.stringify(before)
    applyChatEvent(before, ev('heartbeat', {}))
    applyChatEvent(before, { event: 'delta' }) // data 缺失防御
    expect(JSON.stringify(before)).toBe(snapshot)
  })
})

// W5 HITL（§3.7）：pending_approval / pending_saved 的登记与卡片生命周期
describe('applyChatEvent：HITL 审批事件（W5 §3.7）', () => {
  it('pending_approval：登记 msg.pending，streaming 保持 true（UI 显示"等待确认"）', () => {
    let m = newAssistantMsg()
    m = applyChatEvent(m, ev('tool', { name: 'execute_readonly_sql' }))
    m = applyChatEvent(m, ev('delta', { text: '让我查一下' }))
    m = applyChatEvent(m, ev('pending_approval', { approvalId: 'a-1', sql: 'SELECT SUM(amount) FROM payments' }))
    expect(m.streaming).toBe(true) // 卡片渲染期间气泡仍处"进行中"
    expect(m.pending).toEqual({
      approvalId: 'a-1',
      sql: 'SELECT SUM(amount) FROM payments',
      historyId: null,
      expired: false,
      busy: false,
      resolved: false,
    })
    expect(m.text).toBe('让我查一下') // 已收前言不清除
  })

  it('pending_saved：historyId 绑进 pending（批准请求原地 UPDATE 的依据）', () => {
    let m = newAssistantMsg()
    m = applyChatEvent(m, ev('pending_approval', { approvalId: 'a-1', sql: 'SELECT 1' }))
    m = applyChatEvent(m, ev('pending_saved', { historyId: 77 }))
    expect(m.pending.historyId).toBe(77)
  })

  it('恢复流 delta/done：气泡续写，done 后 pending 置 resolved（卡片退场）', () => {
    let m = newAssistantMsg()
    for (const e of [
      ev('pending_approval', { approvalId: 'a-1', sql: 'SELECT 1' }),
      ev('pending_saved', { historyId: 77 }),
      ev('delta', { text: '，结果是 1。' }),
      ev('done', { tokens: { input: 50, output: 5 }, latencyMs: 200 }),
    ]) {
      m = applyChatEvent(m, e)
    }
    expect(m.text).toBe('，结果是 1。')
    expect(m.streaming).toBe(false)
    expect(m.pending.resolved).toBe(true)
    expect(m.pending.historyId).toBe(77)
  })

  it('恢复流 error：审批已消费无法重试 → pending 置 expired', () => {
    let m = newAssistantMsg()
    m = applyChatEvent(m, ev('pending_approval', { approvalId: 'a-1', sql: 'SELECT 1' }))
    m = applyChatEvent(m, ev('error', { code: 'llm_insufficient_balance', message: 'DeepSeek 余额不足' }))
    expect(m.pending.expired).toBe(true)
    expect(m.errorCode).toBe('llm_insufficient_balance')
  })

  it('pending_saved 在无 pending 时安全忽略（异常序列防御）', () => {
    const before = newAssistantMsg()
    const snapshot = JSON.stringify(before)
    const after = applyChatEvent(before, ev('pending_saved', { historyId: 1 }))
    expect(JSON.stringify(after)).toBe(snapshot) // 纯函数：不修改入参
  })

  it('全流程串跑（HITL）：提问 → 停车 → 审批卡 → 批准 → 续写 → done → saved', () => {
    let m = newAssistantMsg()
    for (const e of [
      ev('meta', { threadKey: 'u42:t1', model: 'deepseek-flash' }),
      ev('delta', { text: '让我查一下' }),
      ev('tool', { name: 'execute_readonly_sql' }),
      ev('pending_approval', { approvalId: 'a-1', sql: "SELECT SUM(amount) FROM payments WHERE paid_at >= '2026-09-01'" }),
      ev('pending_saved', { historyId: 7 }),
      ev('delta', { text: '，上月 GMV 为 1603 万元。' }),
      ev('done', { tokens: { input: 150, output: 15 }, latencyMs: 500 }),
      ev('saved', { historyId: 7 }),
    ]) {
      m = applyChatEvent(m, e)
    }
    expect(m.text).toBe('让我查一下，上月 GMV 为 1603 万元。')
    expect(m.historyId).toBe(7) // saved 与 pending_saved 同值：反馈按钮解锁
    expect(m.pending.resolved).toBe(true)
    expect(m.streaming).toBe(false)
  })
})

// 线上 bug 回归锁（2026-10-11）：pushAssistant 必须返回 store 数组内的响应式代理，
// 而非 push 进去的原始对象——原始引用上的 Object.assign 绕过 Pinia reactive 拦截，
// 导致流式文本不渲染（光标闪烁直到流结束）。
describe('流式响应性契约（回归）', () => {
  it('pushAssistant 返回数组内的响应式引用：外部 mutations 必须可见于 messages', async () => {
    const { createPinia, setActivePinia } = await import('pinia')
    setActivePinia(createPinia())
    const { useChatStore, newAssistantMsg } = await import('./chat.js')
    const store = useChatStore()
    const raw = newAssistantMsg()
    const proxied = store.pushAssistant(raw)
    expect(proxied).not.toBe(raw)            // 返回的必须是 reactive 包装
    expect(store.messages.at(-1)).toBe(proxied)
    proxied.text = 'abc'                      // 外部写代理必须反映到 store
    expect(store.messages.at(-1).text).toBe('abc')
  })
})

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

/**
 * 对话 store：消息数组、流状态、threadId。
 * applyChatEvent 是纯 reducer（可单测）：SSE 事件 → 助手消息状态。
 * 工具事件规则（与 agent/server.py 事件契约对齐）：
 * - 起始 tool 事件 {"name"}：同名只保留一条"正在查询…"指示行（去重，后端同轮本就只发一次）
 * - 完成 tool 事件 {"name","sql"}：更新同名指示行为可折叠"已执行 SQL"（默认收起）
 */
import { defineStore } from 'pinia'
import { messageFor } from '../api/errors'
import { uuid } from '../utils/format'

export const FEEDBACK_WINDOW_MS = 60 * 1000 // done 后 60s 未收到 saved → 反馈按钮禁用

export function newAssistantMsg() {
  return {
    role: 'assistant',
    text: '',
    streaming: true,
    stopped: false, // 用户主动停止
    interrupted: false, // 网络中断（非 abort）
    errorCode: null,
    errorMessage: '',
    tools: [], // [{name, sql|null}] sql 非空 = 已完成，可折叠展示
    model: '',
    tokens: null, // {input, output}
    latencyMs: null,
    generatedSql: '', // done 事件携带的最后一轮 SQL（冗余自 tools，便于历史对照）
    historyId: null, // saved 事件注入；有值才显示点赞/点踩
    feedbackExpired: false, // 60s 窗口内未收到 saved
    feedback: null, // 1 赞 / 0 踩 / null 未评
  }
}

/**
 * 纯 reducer：返回新消息对象（不修改入参），方便 vitest 直接断言。
 * @param {{event:string, data:object}} ev
 */
export function applyChatEvent(prev, ev) {
  const m = { ...prev, tools: prev.tools.slice() }
  const d = ev.data || {}
  switch (ev.event) {
    case 'meta':
      m.model = d.model || m.model
      break
    case 'delta':
      m.text += d.text ?? ''
      break
    case 'tool': {
      if (d.sql) {
        // 完成事件：更新最后一个同名指示行为"已执行 SQL"
        const target = [...m.tools].reverse().find((t) => t.name === d.name)
        if (target) target.sql = d.sql
        else m.tools.push({ name: d.name, sql: d.sql })
      } else {
        // 起始事件：同名去重，保持一条"正在查询…"指示行
        if (!m.tools.some((t) => t.name === d.name)) {
          m.tools.push({ name: d.name, sql: null })
        }
      }
      break
    }
    case 'done':
      m.streaming = false
      m.tokens = d.tokens || m.tokens
      m.latencyMs = d.latencyMs ?? m.latencyMs
      if (d.generatedSql) m.generatedSql = d.generatedSql
      break
    case 'error':
      m.streaming = false
      m.errorCode = d.code || 'internal_error'
      m.errorMessage = messageFor(d.code, d.message)
      break
    case 'saved':
      m.historyId = d.historyId ?? m.historyId
      break
    default:
      break // 未知事件忽略，不崩
  }
  return m
}

export const useChatStore = defineStore('chat', {
  state: () => ({
    threadId: uuid(),
    messages: [], // [{role:'user'|'assistant', ...}]
    streaming: false, // 全局互斥：一条流进行中禁止再次发送
  }),

  actions: {
    newThread() {
      this.threadId = uuid()
      this.messages = []
      this.streaming = false
    },

    pushUser(text) {
      this.messages.push({ role: 'user', text })
    },

    pushAssistant(msg) {
      this.messages.push(msg)
      return msg
    },

    /** 流事件落消息：reducer 出新对象，合并回响应式消息 */
    applyEvent(msg, ev) {
      Object.assign(msg, applyChatEvent(msg, ev))
    },

    markStopped(msg) {
      msg.streaming = false
      msg.stopped = true
    },

    markInterrupted(msg) {
      msg.streaming = false
      msg.interrupted = true
    },

    /** done 后开启 60s 反馈窗口：到期仍未收到 saved → 按钮禁用 */
    armFeedbackWindow(msg) {
      if (msg.historyId != null) return
      setTimeout(() => {
        if (msg.historyId == null) msg.feedbackExpired = true
      }, FEEDBACK_WINDOW_MS)
    },
  },
})

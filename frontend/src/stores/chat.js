/**
 * 对话 store：消息数组、流状态、threadId。
 * applyChatEvent 是纯 reducer（可单测）：SSE 事件 → 助手消息状态。
 * 工具事件规则（与 agent/server.py 事件契约对齐——后端按工具调用槽（index）各发一条，
 * 多 action 轮同名工具各有起始/完成事件，W7 设计 §3.1）：
 * - 起始 tool 事件 {"name"}：存在同名"正在查询…"指示行时去重；全部已完成（或无同名行）
 *   则追加新行——绝不把已完成行复活回"正在查询"态
 * - 完成 tool 事件 {"name","sql"}：落在最后一个同名未完成行上（→ 可折叠"已执行 SQL"，
 *   默认收起）；无未完成行则追加新行——已完成行不覆写（多 action 轮每条 SQL 一条指示行，
 *   与真实执行序列对齐）
 * HITL 事件（W5 §3.7）：
 * - pending_approval {approvalId, sql, actionCount}：登记 msg.pending（审批卡渲染），
 *   streaming 保持 true；actionCount>1 时审批卡显示"批 1 拒余"降级文案（W7 §3.1）
 * - pending_saved {historyId}：把 historyId 绑进 pending（批准/拒绝时随请求带回原地 UPDATE）
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
    pending: null, // W5 HITL：{approvalId, sql, actionCount, historyId, expired, busy, resolved}
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
        // 完成事件：找最后一个同名且未完成（sql 为空）的指示行落 SQL（"正在查询"→"已完成"）。
        // 已完成行不覆写（W7 §3.1：多 action 轮完成事件逐条到达）→ 无未完成行时追加新行
        const target = [...m.tools].reverse().find((t) => t.name === d.name && t.sql == null)
        if (target) target.sql = d.sql
        else m.tools.push({ name: d.name, sql: d.sql })
      } else {
        // 起始事件：仅与同名"正在查询"行去重；全部已完成（或无同名行）→ 追加新行
        // （链式审批轮模型重新发起 SQL：新起一行，不复活已完成行）
        if (!m.tools.some((t) => t.name === d.name && t.sql == null)) {
          m.tools.push({ name: d.name, sql: null })
        }
      }
      break
    }
    case 'pending_approval':
      // W5 §3.7：登记审批卡；streaming 保持 true（气泡仍处"进行中"，等用户决策）
      m.pending = {
        approvalId: d.approvalId ?? '',
        sql: d.sql ?? '',
        actionCount: d.actionCount ?? 1, // W7 §3.1：多 action 轮 >1，审批卡据此显示降级文案
        historyId: null, // pending_saved 到达后绑定
        expired: false, // 409/410 或恢复流出错后置灰
        busy: false, // 审批请求进行中（按钮 loading + 防双击）
        resolved: false, // 恢复流 done 后置 true → 卡片隐藏（SQL 由工具指示行/历史页承载）
      }
      break
    case 'pending_saved':
      if (m.pending) m.pending = { ...m.pending, historyId: d.historyId ?? null }
      break
    case 'done':
      m.streaming = false
      m.tokens = d.tokens || m.tokens
      m.latencyMs = d.latencyMs ?? m.latencyMs
      if (d.generatedSql) m.generatedSql = d.generatedSql
      // 恢复轮收尾：审批已消费 → 卡片退场（工具指示行已展示"已执行 SQL"）
      if (m.pending) m.pending = { ...m.pending, resolved: true }
      break
    case 'error':
      m.streaming = false
      m.errorCode = d.code || 'internal_error'
      m.errorMessage = messageFor(d.code, d.message)
      // 恢复流出错（审批已被消费，无法重试）→ 卡片置灰引导重新提问
      if (m.pending) m.pending = { ...m.pending, expired: true }
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
      // 返回 store 数组内的响应式代理（不是原始对象）——流式更新必须走代理才能触发渲染。
      // 教训（线上 bug 实录）：ChatView 持有 push 前的原始引用去 Object.assign，
      // 绕过了 Pinia reactive 的 set 拦截 → Vue 收不到变更 → 光标闪烁但文本不出现，
      // 直到其他响应式变更（streaming=false）触发整表重渲染才显示。
      return this.messages[this.messages.length - 1]
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

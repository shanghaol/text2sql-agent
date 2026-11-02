<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import MessageBubble from '../components/MessageBubble.vue'
import { chatSSE } from '../api/sse'
import api from '../api'
import { useAuthStore } from '../stores/auth'
import { useChatStore, newAssistantMsg } from '../stores/chat'

const auth = useAuthStore()
const chat = useChatStore()

const draft = ref('')
const listRef = ref()
let controller = null // AbortController：停止按钮 → 标记"已停止"

const canSend = computed(() => !chat.streaming && draft.value.trim().length > 0)

// W5 §3.7：存在待决策的审批卡（未过期/未处理/非请求中）→ 停止=拒绝、新对话禁用、新提问拦截
const pendingMsg = computed(() =>
  chat.messages.find((m) => m.role === 'assistant' && m.pending && !m.pending.resolved && !m.pending.expired && !m.pending.busy),
)
const hasPending = computed(() => !!pendingMsg.value)

// 流式输出时自动滚到底部
const lastTextLen = computed(() => {
  const m = chat.messages[chat.messages.length - 1]
  return m ? m.text.length : 0
})
watch([lastTextLen, () => chat.messages.length], () => nextTick(scrollToBottom))

function scrollToBottom() {
  const el = listRef.value
  if (el) el.scrollTop = el.scrollHeight
}

async function send() {
  const question = draft.value.trim()
  if (!question || chat.streaming) return
  if (hasPending.value) {
    // 后端本也会 409 pending_approval_exists——本地先拦，交互更顺（§3.10）
    ElMessage.warning('当前会话有等待确认的 SQL，请先处理审批卡片；若刷新后卡片已丢失，请等审批过期（约 5 分钟）后重新提问')
    return
  }
  if (!auth.hasKey) {
    ElMessage.warning('请先到设置页配置 DeepSeek Key')
    return
  }
  draft.value = ''
  chat.pushUser(question)
  const msg = chat.pushAssistant(newAssistantMsg()) // 响应式代理引用（见 chat.js pushAssistant 教训注释）
  chat.streaming = true
  controller = new AbortController()

  try {
    // §4.3：开流前本地 exp 预检刷新，保证 SSE 建立时 token 新鲜
    await auth.ensureFreshToken()
    await chatSSE({
      question,
      threadId: chat.threadId,
      token: auth.accessToken,
      signal: controller.signal,
      onEvent: (ev) => chat.applyEvent(msg, ev),
    })
  } catch (e) {
    if (e && e.name === 'AbortError') {
      chat.markStopped(msg) // §4.2 要点 4
    } else if (e.code === 'key_not_set') {
      chat.markInterrupted(msg)
      ElMessage({ message: e.message, type: 'warning', duration: 4000 })
    } else {
      // 网络中断 / 流前业务错误：不清除已收文本（§4.2 要点 5）
      chat.markInterrupted(msg)
      ElMessage.error(e.message)
    }
  } finally {
    chat.streaming = false
    controller = null
    // 60s 内未收到 saved → 反馈按钮禁用（chat.armFeedbackWindow）
    chat.armFeedbackWindow(msg)
    nextTick(scrollToBottom)
  }
}

/** W5 §3.7：批准/拒绝 → POST /api/chat/approval → 复用 sse.js 解析器消费恢复流，同气泡续写。 */
async function decide(msg, decision) {
  const p = msg.pending
  if (!p || p.busy || p.expired || p.resolved) return
  if (p.historyId == null) return // pending_saved 未到（卡片按钮此时本已禁用，防御兜底）
  p.busy = true
  chat.streaming = true
  controller = new AbortController()
  try {
    await auth.ensureFreshToken()
    await chatSSE({
      path: '/api/chat/approval',
      body: {
        threadId: chat.threadId,
        approvalId: p.approvalId,
        decision,
        historyId: p.historyId,
      },
      token: auth.accessToken,
      signal: controller.signal,
      onEvent: (ev) => chat.applyEvent(msg, ev),
    })
  } catch (e) {
    if (e && e.name === 'AbortError') {
      chat.markStopped(msg) // 恢复流中途停止（Java 按 status=3 落库）
    } else if (e.code === 'approval_not_found' || e.code === 'approval_expired') {
      p.expired = true // 审批已消费/过期 → 卡片置灰（双击批准的第二击走这里）
      ElMessage.warning(e.message)
    } else {
      chat.markInterrupted(msg)
      ElMessage.error(e.message)
    }
  } finally {
    chat.streaming = false
    controller = null
    chat.armFeedbackWindow(msg)
    nextTick(scrollToBottom)
  }
}

function stop() {
  // W5 §3.7：pending 期间「停止」= 发送 reject（通知后端取消，模型收尾询问意图）
  if (pendingMsg.value && !chat.streaming) {
    decide(pendingMsg.value, 'reject')
    return
  }
  controller?.abort()
}

function newThread() {
  if (chat.streaming) controller?.abort()
  chat.newThread() // 重生 threadId uuid + 清空本地消息
}

async function sendFeedback(msg, rating) {
  try {
    await api.post('/feedback', { historyId: msg.historyId, rating })
    msg.feedback = rating // 重复点击/换边都走后端 upsert 更新
  } catch (e) {
    ElMessage.error(e.message)
  }
}

onBeforeUnmount(() => controller?.abort())
</script>

<template>
  <div class="chat-page">
    <!-- hasKey=false：顶部常驻横幅引导去设置页（§4.1） -->
    <el-alert v-if="!auth.hasKey" type="warning" :closable="false" class="key-banner">
      尚未配置 DeepSeek Key，对话前请先
      <router-link to="/settings">去设置</router-link>
    </el-alert>

    <div ref="listRef" class="msg-list">
      <el-empty v-if="chat.messages.length === 0" description="输入自然语言问题，查询 mart 业务库" />
      <MessageBubble
        v-for="(m, i) in chat.messages"
        :key="i"
        :msg="m"
        @feedback="sendFeedback"
        @approve="(mm) => decide(mm, 'approve')"
        @reject="(mm) => decide(mm, 'reject')"
      />
    </div>

    <div class="composer">
      <el-input
        v-model="draft"
        type="textarea"
        :rows="2"
        resize="none"
        maxlength="2000"
        show-word-limit
        placeholder="例如：上个月的 GMV 是多少？（Enter 发送，Shift+Enter 换行）"
        @keydown.enter.exact.prevent="send"
      />
      <div class="composer-actions">
        <!-- pending 期间禁用新对话：避免新旧 thread 状态交叉（§3.7） -->
        <el-button :disabled="chat.streaming || hasPending" @click="newThread">新对话</el-button>
        <el-button v-if="chat.streaming || hasPending" type="danger" @click="stop">停止</el-button>
        <el-button v-else type="primary" :disabled="!canSend" @click="send">发送</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chat-page {
  height: 100%;
  display: flex;
  flex-direction: column;
}
.key-banner {
  border-radius: 0;
}
.msg-list {
  flex: 1;
  overflow-y: auto;
  padding: 12px 0;
}
.composer {
  border-top: 1px solid var(--el-border-color-light);
  padding: 12px 24px 16px;
  background: #fff;
}
.composer-actions {
  margin-top: 8px;
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>

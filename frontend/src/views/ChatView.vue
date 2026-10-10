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
  if (!auth.hasKey) {
    ElMessage.warning('请先到设置页配置 DeepSeek Key')
    return
  }
  draft.value = ''
  chat.pushUser(question)
  const msg = chat.pushAssistant(newAssistantMsg())
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

function stop() {
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
      <MessageBubble v-for="(m, i) in chat.messages" :key="i" :msg="m" @feedback="sendFeedback" />
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
        <el-button @click="newThread" :disabled="chat.streaming">新对话</el-button>
        <el-button v-if="!chat.streaming" type="primary" :disabled="!canSend" @click="send">发送</el-button>
        <el-button v-else type="danger" @click="stop">停止</el-button>
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

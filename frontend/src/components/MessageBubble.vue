<script setup>
import { computed } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import ToolIndicator from './ToolIndicator.vue'
import ApprovalCard from './ApprovalCard.vue'

const props = defineProps({
  msg: { type: Object, required: true },
})
// feedback：点赞/点踩；approve/reject：HITL 审批决策（W5 §3.7，ChatView 决定如何发请求）
defineEmits(['feedback', 'approve', 'reject'])

const html = computed(() => {
  try {
    return DOMPurify.sanitize(marked.parse(props.msg.text || '', { breaks: true }))
  } catch {
    return ''
  }
})
</script>

<template>
  <div class="bubble-row" :class="msg.role">
    <div class="bubble" :class="msg.role">
      <!-- 助手消息：工具指示行 → 审批卡 → markdown 正文 → 状态/反馈区 -->
      <template v-if="msg.role === 'assistant'">
        <ToolIndicator v-for="(t, i) in msg.tools" :key="i" :tool="t" />
        <!-- W5 HITL：待确认 SQL 的审批卡（done 后 resolved → 退场） -->
        <ApprovalCard
          v-if="msg.pending && !msg.pending.resolved"
          :pending="msg.pending"
          @approve="$emit('approve', msg)"
          @reject="$emit('reject', msg)"
        />
        <div v-if="msg.text" class="md" v-html="html" />
        <div v-if="msg.streaming" class="cursor">▍</div>

        <el-alert
          v-if="msg.errorCode"
          :title="msg.errorMessage"
          :type="msg.errorCode === 'llm_key_invalid' || msg.errorCode === 'llm_insufficient_balance' ? 'warning' : 'error'"
          show-icon
          :closable="false"
          class="status"
        />
        <div v-else-if="msg.stopped" class="status muted">⏹ 已停止</div>
        <div v-else-if="msg.interrupted" class="status muted">⚠ 连接中断，可重试</div>

        <div v-if="!msg.streaming" class="meta-line">
          <span v-if="msg.latencyMs != null" class="dim">耗时 {{ (msg.latencyMs / 1000).toFixed(1) }}s</span>
          <span v-if="msg.tokens" class="dim">tokens {{ msg.tokens.input }}/↑{{ msg.tokens.output }}</span>
        </div>

        <!-- 点赞/点踩：仅 saved 注入 historyId 且未过 60s 窗口时可用；重复点击走更新（upsert） -->
        <div v-if="msg.role === 'assistant' && !msg.streaming" class="feedback">
          <template v-if="msg.historyId != null && !msg.feedbackExpired">
            <el-button
              link
              size="small"
              :type="msg.feedback === 1 ? 'primary' : 'default'"
              @click="$emit('feedback', msg, 1)"
            >👍 有用</el-button>
            <el-button
              link
              size="small"
              :type="msg.feedback === 0 ? 'danger' : 'default'"
              @click="$emit('feedback', msg, 0)"
            >👎 没用</el-button>
          </template>
        </div>
      </template>

      <!-- 用户消息：纯文本 -->
      <template v-else>{{ msg.text }}</template>
    </div>
  </div>
</template>

<style scoped>
.bubble-row {
  display: flex;
  padding: 8px 24px;
}
.bubble-row.user {
  justify-content: flex-end;
}
.bubble {
  max-width: 78%;
  border-radius: 10px;
  padding: 10px 14px;
  line-height: 1.65;
}
.bubble.user {
  background: var(--el-color-primary-light-8);
}
.bubble.assistant {
  background: var(--el-fill-color-light);
}
.cursor {
  display: inline-block;
  color: var(--el-color-primary);
  animation: blink 1s step-start infinite;
}
@keyframes blink {
  50% {
    opacity: 0;
  }
}
.status {
  margin-top: 6px;
}
.muted {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.meta-line {
  margin-top: 6px;
  display: flex;
  gap: 12px;
}
.dim {
  color: var(--el-text-color-placeholder);
  font-size: 12px;
}
.feedback {
  margin-top: 4px;
  display: flex;
  gap: 4px;
}

/* markdown 内容基础排版 */
.md :deep(p) {
  margin: 4px 0;
}
.md :deep(pre) {
  background: #282c34;
  color: #abb2bf;
  padding: 10px 12px;
  border-radius: 6px;
  overflow-x: auto;
  font-size: 13px;
}
.md :deep(code) {
  font-family: ui-monospace, Consolas, monospace;
}
.md :deep(:not(pre) > code) {
  background: var(--el-fill-color-darker);
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 13px;
}
.md :deep(table) {
  border-collapse: collapse;
  margin: 6px 0;
}
.md :deep(th),
.md :deep(td) {
  border: 1px solid var(--el-border-color);
  padding: 4px 10px;
  font-size: 13px;
}
.md :deep(ul),
.md :deep(ol) {
  padding-left: 20px;
  margin: 4px 0;
}
</style>

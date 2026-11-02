<script setup>
/**
 * HITL 审批卡（W5 设计 §3.7）：展示待执行 SQL（只读）+ 批准/拒绝按钮。
 * - msg.pending 存在且未 resolved 时由 MessageBubble 条件渲染；
 * - pending_saved 注入 historyId 前按钮禁用（批准请求需要它做原地 UPDATE）；
 * - expired（409/410/恢复流出错）→ 置灰态，提示重新提问；
 * - busy（审批请求进行中）→ 按钮 loading + 双击防护；
 * - actionCount>1（多 action 轮，W7 设计 §3.1 降级文案）：卡片只展示/确认第 1 条 SQL，
 *   其余按"批 1 拒余"兜底（approval.decisions_for），文案把该行为对用户透明化——
 *   被拒 SQL 如仍需要，模型重新发起 → 链式新审批卡（逐条审批 UI 记 backlog）。
 */
defineProps({
  pending: { type: Object, required: true },
})

defineEmits(['approve', 'reject'])
</script>

<template>
  <div class="approval-card" :class="{ expired: pending.expired }">
    <div class="head">
      <span class="title">SQL 执行确认</span>
      <span v-if="pending.expired" class="badge expired">已过期</span>
      <span v-else-if="pending.resolved" class="badge">已处理</span>
      <span v-else class="badge waiting">等待您确认</span>
    </div>
    <div v-if="pending.actionCount > 1" class="multi-note">
      本轮共 {{ pending.actionCount }} 条 SQL：批准第 1 条、拒绝其余；被拒的 SQL 如仍需要，助手会重新发起并再次请求审批
    </div>
    <pre class="sql">{{ pending.sql || '-- （后端未提供 SQL）' }}</pre>
    <div v-if="!pending.expired" class="ops">
      <el-button
        type="primary"
        size="small"
        :loading="pending.busy"
        :disabled="pending.historyId == null"
        @click="$emit('approve')"
      >批准执行</el-button>
      <el-button
        type="danger"
        plain
        size="small"
        :disabled="pending.busy || pending.historyId == null"
        @click="$emit('reject')"
      >拒绝</el-button>
      <span v-if="pending.historyId == null" class="dim">（正在登记…）</span>
    </div>
    <div v-else class="muted">审批已失效，请重新提问</div>
  </div>
</template>

<style scoped>
.approval-card {
  margin: 6px 0;
  padding: 10px 12px;
  border: 1px solid var(--el-color-warning-light-5);
  border-radius: 8px;
  background: var(--el-color-warning-light-9);
}
.approval-card.expired {
  border-color: var(--el-border-color-light);
  background: var(--el-fill-color-light);
  opacity: 0.75;
}
.head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 600;
}
.badge {
  font-weight: 400;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.badge.waiting {
  color: var(--el-color-warning);
}
.badge.expired {
  color: var(--el-text-color-placeholder);
}
.multi-note {
  margin-bottom: 8px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-color-warning-dark-2);
}
.sql {
  margin: 0 0 8px;
  padding: 8px 10px;
  background: #282c34;
  color: #98c379;
  border-radius: 6px;
  font-size: 13px;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
  user-select: text;
}
.ops {
  display: flex;
  align-items: center;
  gap: 8px;
}
.dim,
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>

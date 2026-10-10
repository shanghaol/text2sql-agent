<script setup>
defineProps({
  tool: { type: Object, required: true }, // {name, sql|null}
})

const LABELS = {
  execute_readonly_sql: '正在查询数据库…',
  search_query_history: '正在检索历史记录…',
}
function runningLabel(name) {
  return LABELS[name] || `正在调用工具 ${name}…`
}
</script>

<template>
  <!-- 进行中：单行指示（同名去重由 chat store mapper 保证） -->
  <div v-if="!tool.sql" class="tool-line">⚙ {{ runningLabel(tool.name) }}</div>
  <!-- 完成且带 SQL：可折叠"已执行 SQL"，默认收起 -->
  <el-collapse v-else class="tool-sql">
    <el-collapse-item :title="'⚙ 已执行 SQL'" :name="tool.name">
      <pre class="sql-block">{{ tool.sql }}</pre>
    </el-collapse-item>
  </el-collapse>
</template>

<style scoped>
.tool-line {
  color: var(--el-color-info);
  font-size: 13px;
  padding: 2px 0;
}
.tool-sql {
  border-top: none;
  border-bottom: none;
  margin: 2px 0;
}
.tool-sql :deep(.el-collapse-item__header) {
  color: var(--el-color-info);
  font-size: 13px;
  height: 28px;
  border-bottom: none;
}
.tool-sql :deep(.el-collapse-item__wrap) {
  border-bottom: none;
}
.sql-block {
  margin: 0;
  padding: 8px 10px;
  background: #282c34;
  color: #98c379;
  border-radius: 6px;
  font-size: 12.5px;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>

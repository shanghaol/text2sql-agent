<script setup>
/**
 * 历史记录页（§4.1）：分页表格（时间/问题/状态徽标）+ 行点击抽屉
 * （answer 全文 markdown + generated_sql 代码块 + 耗时/token）。
 */
import { onMounted, reactive, ref } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import api from '../api'
import { fmtDateTime } from '../utils/format'

const PAGE_SIZE = 20

const loading = ref(false)
const page = reactive({ current: 1, total: 0 })
const records = ref([])
const drawer = ref(false)
const detail = ref(null)
const detailLoading = ref(false)

const STATUS_META = {
  1: { label: '成功', type: 'success' },
  2: { label: '失败', type: 'danger' },
  3: { label: '已中断', type: 'info' },
  4: { label: '待审批', type: 'info' }, // W5 §9-5：灰标签；批准后正常化 / 拒绝后沿用 status=3 文案族
}

async function load() {
  loading.value = true
  try {
    const body = await api.get('/history', { params: { page: page.current, size: PAGE_SIZE } })
    page.total = body.data.total
    records.value = body.data.records
  } finally {
    loading.value = false
  }
}

onMounted(load)

async function openRow(row) {
  drawer.value = true
  detailLoading.value = true
  detail.value = null
  try {
    const body = await api.get(`/history/${row.id}`)
    detail.value = body.data
  } finally {
    detailLoading.value = false
  }
}

const answerHtml = () => {
  try {
    return DOMPurify.sanitize(marked.parse(detail.value?.answerText || '', { breaks: true }))
  } catch {
    return ''
  }
}
</script>

<template>
  <div class="history-page">
    <el-table :data="records" v-loading="loading" highlight-current-row style="cursor: pointer" @row-click="openRow">
      <el-table-column label="时间" width="170">
        <template #default="{ row }">{{ fmtDateTime(row.createdAt) }}</template>
      </el-table-column>
      <el-table-column prop="question" label="问题" min-width="300" show-overflow-tooltip />
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="STATUS_META[row.status]?.type || 'info'" size="small">
            {{ STATUS_META[row.status]?.label || '未知' }}
          </el-tag>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      class="pager"
      layout="total, prev, pager, next"
      :total="page.total"
      :page-size="PAGE_SIZE"
      :current-page="page.current"
      @current-change="(p) => { page.current = p; load() }"
    />

    <el-drawer v-model="drawer" title="查询详情" size="46%">
      <div v-loading="detailLoading" class="detail">
        <template v-if="detail">
          <h4>问题</h4>
          <p>{{ detail.question }}</p>
          <h4>回答</h4>
          <div class="md answer" v-html="answerHtml()" />
          <template v-if="detail.generatedSql">
            <h4>生成的 SQL</h4>
            <pre class="sql-block">{{ detail.generatedSql }}</pre>
          </template>
          <h4>指标</h4>
          <p class="metrics">
            <el-tag size="small" :type="STATUS_META[detail.status]?.type || 'info'">
              {{ STATUS_META[detail.status]?.label || '未知' }}
            </el-tag>
            <span v-if="detail.latencyMs != null">耗时 {{ (detail.latencyMs / 1000).toFixed(1) }}s</span>
            <span v-if="detail.tokenIn != null">输入 {{ detail.tokenIn }} tokens</span>
            <span v-if="detail.tokenOut != null">输出 {{ detail.tokenOut }} tokens</span>
          </p>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.history-page {
  height: 100%;
  padding: 20px 24px;
  overflow-y: auto;
}
.pager {
  margin-top: 14px;
  justify-content: flex-end;
}
.detail h4 {
  margin: 14px 0 8px;
}
.detail h4:first-child {
  margin-top: 0;
}
.metrics {
  display: flex;
  align-items: center;
  gap: 14px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.answer :deep(p) {
  margin: 4px 0;
  line-height: 1.7;
}
.answer :deep(pre) {
  background: #282c34;
  color: #abb2bf;
  padding: 10px 12px;
  border-radius: 6px;
  overflow-x: auto;
  font-size: 13px;
}
.sql-block {
  margin: 0;
  padding: 10px 12px;
  background: #282c34;
  color: #98c379;
  border-radius: 6px;
  font-size: 13px;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>

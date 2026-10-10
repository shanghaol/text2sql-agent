<script setup>
/**
 * BYOK 设置页（W4 设计 §1.2 五步流程 + §4.1 三态）：
 * 未配置（密码框+明文切换+提交）/ 已配置（掩码+指纹+上次校验时间+重新校验+删除）/ 校验失败红字。
 * 前端格式预检 ^sk-[0-9a-zA-Z]{20,}$ 只挡手滑，权威校验在 Java → DeepSeek /models。
 */
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'
import { useAuthStore } from '../stores/auth'
import { fmtDateTime } from '../utils/format'

const auth = useAuthStore()

const status = ref(null) // {configured, masked, fingerprint, lastVerifiedAt}
const loading = ref(true)
const submitting = ref(false)
const verifying = ref(false)
const localError = ref('') // 前端预检失败（不发请求）
const serverError = ref('') // 校验失败红字（key_invalid / verify_rate_limited / verify_unreachable / bad_key_format）

const form = reactive({ key: '' })

const KEY_PATTERN = /^sk-[0-9a-zA-Z]{20,}$/

async function loadStatus() {
  loading.value = true
  try {
    const body = await api.get('/user/key')
    status.value = body.data
    auth.setUserHasKey(!!body.data?.configured)
  } finally {
    loading.value = false
  }
}

onMounted(loadStatus)

async function submitKey() {
  localError.value = ''
  serverError.value = ''
  const key = form.key.trim()
  // 五步流程第 1 步：前端格式预检，不过就地红字、不发请求
  if (!KEY_PATTERN.test(key)) {
    localError.value = 'Key 格式不正确：应以 sk- 开头，后接至少 20 位字母或数字'
    return
  }
  submitting.value = true
  try {
    await api.put('/user/key', { key })
    ElMessage.success('配置成功')
    form.key = ''
    await loadStatus()
  } catch (e) {
    // 第 4 步：校验失败不落库，按 §1.2 文案表红字提示
    serverError.value = e.message
  } finally {
    submitting.value = false
  }
}

async function reverify() {
  verifying.value = true
  serverError.value = ''
  try {
    const body = await api.post('/user/key/verify')
    if (body.data?.valid) ElMessage.success(`校验通过（${fmtDateTime(body.data.verifiedAt)}）`)
    await loadStatus()
  } catch (e) {
    serverError.value = e.message
  } finally {
    verifying.value = false
  }
}

async function removeKey() {
  try {
    await ElMessageBox.confirm('删除后需重新录入才能继续对话，确认删除？', '删除 Key', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    await api.delete('/user/key')
    ElMessage.success('已删除')
    await loadStatus()
  } catch (e) {
    ElMessage.error(e.message)
  }
}
</script>

<template>
  <div class="settings-page">
    <el-card class="card" v-loading="loading">
      <template #header><b>DeepSeek API Key（BYOK）</b></template>

      <!-- 态一：未配置 -->
      <template v-if="!status?.configured">
        <p class="hint">
          Key 仅 yourself 可见：服务端加密存储、任何接口不回显明文；对话时按请求解密使用。
          <a href="https://platform.deepseek.com/api_keys" target="_blank" rel="noopener">前往 DeepSeek 平台获取 →</a>
        </p>
        <el-form @submit.prevent="submitKey">
          <el-form-item>
            <el-input
              v-model="form.key"
              type="password"
              show-password
              placeholder="sk-..."
              autocomplete="off"
              class="key-input"
            />
          </el-form-item>
          <el-button type="primary" :loading="submitting" @click="submitKey">保存并校验</el-button>
        </el-form>
      </template>

      <!-- 态二：已配置 -->
      <template v-else>
        <div class="kv">
          <span class="k">已配置</span>
          <el-tag type="success">{{ status.masked }}</el-tag>
        </div>
        <div class="kv">
          <span class="k">指纹</span>
          <code>{{ status.fingerprint }}</code>
        </div>
        <div class="kv">
          <span class="k">上次校验</span>
          <span>{{ fmtDateTime(status.lastVerifiedAt) }}</span>
        </div>
        <div class="ops">
          <el-button :loading="verifying" @click="reverify">重新校验</el-button>
          <el-button type="danger" plain @click="removeKey">删除</el-button>
        </div>
      </template>

      <!-- 态三：校验失败红字（§1.2 文案表映射自 errors.js） -->
      <el-alert v-if="localError" :title="localError" type="error" show-icon :closable="false" class="err" />
      <el-alert v-if="serverError" :title="serverError" type="error" show-icon :closable="false" class="err" />
    </el-card>
  </div>
</template>

<style scoped>
.settings-page {
  height: 100%;
  display: flex;
  justify-content: center;
  padding: 32px 24px;
  overflow-y: auto;
}
.card {
  width: 560px;
  height: fit-content;
}
.hint {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.7;
}
.key-input {
  max-width: 420px;
}
.kv {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 10px 0;
}
.k {
  width: 72px;
  color: var(--el-text-color-secondary);
}
.ops {
  margin-top: 18px;
  display: flex;
  gap: 8px;
}
.err {
  margin-top: 14px;
}
</style>

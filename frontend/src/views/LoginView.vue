<script setup>
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import api from '../api'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()

const activeTab = ref('login')
const loading = ref(false)

const USERNAME_PATTERN = /^[A-Za-z0-9_]{4,32}$/ // 同构后端 §2.3 规则

const loginForm = reactive({ username: '', password: '' })
const regForm = reactive({ username: '', password: '', confirm: '' })

const loginRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { pattern: USERNAME_PATTERN, message: '4-32 位字母、数字或下划线', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 8, message: '密码至少 8 位', trigger: 'blur' },
  ],
}
const regRules = {
  ...loginRules,
  confirm: [
    { required: true, message: '请再次输入密码', trigger: 'blur' },
    {
      validator: (_r, v, cb) => (v === regForm.password ? cb() : cb(new Error('两次输入的密码不一致'))),
      trigger: 'blur',
    },
  ],
}

const loginRef = ref()
const regRef = ref()

async function doLogin() {
  await loginRef.value.validate()
  loading.value = true
  try {
    const body = await api.post('/auth/login', { ...loginForm })
    auth.setSession(body.data)
    ElMessage.success('登录成功')
    router.push(route.query.redirect || '/chat')
  } finally {
    loading.value = false
  }
}

async function doRegister() {
  await regRef.value.validate()
  loading.value = true
  try {
    const body = await api.post('/auth/register', { username: regForm.username, password: regForm.password })
    auth.setSession(body.data) // 注册即登录（§2.4：直接发 token）
    ElMessage.success('注册成功，已自动登录')
    router.push(route.query.redirect || '/chat')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <el-card class="login-card">
      <h2 class="title">Text2SQL 分析助手</h2>
      <p class="subtitle">用自然语言查询数据库</p>
      <el-tabs v-model="activeTab" stretch>
        <el-tab-pane label="登录" name="login">
          <el-form ref="loginRef" :model="loginForm" :rules="loginRules" label-position="top" @keyup.enter="doLogin">
            <el-form-item label="用户名" prop="username">
              <el-input v-model="loginForm.username" placeholder="4-32 位字母、数字或下划线" autocomplete="username" />
            </el-form-item>
            <el-form-item label="密码" prop="password">
              <el-input v-model="loginForm.password" type="password" show-password placeholder="至少 8 位" autocomplete="current-password" />
            </el-form-item>
            <el-button type="primary" class="submit" :loading="loading" @click="doLogin">登 录</el-button>
          </el-form>
        </el-tab-pane>
        <el-tab-pane label="注册" name="register">
          <el-form ref="regRef" :model="regForm" :rules="regRules" label-position="top" @keyup.enter="doRegister">
            <el-form-item label="用户名" prop="username">
              <el-input v-model="regForm.username" placeholder="4-32 位字母、数字或下划线" autocomplete="username" />
            </el-form-item>
            <el-form-item label="密码" prop="password">
              <el-input v-model="regForm.password" type="password" show-password placeholder="至少 8 位" autocomplete="new-password" />
            </el-form-item>
            <el-form-item label="确认密码" prop="confirm">
              <el-input v-model="regForm.confirm" type="password" show-password placeholder="再次输入密码" autocomplete="new-password" />
            </el-form-item>
            <el-button type="primary" class="submit" :loading="loading" @click="doRegister">注 册</el-button>
          </el-form>
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<style scoped>
.login-page {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--el-fill-color-light);
}
.login-card {
  width: 400px;
}
.title {
  text-align: center;
  margin: 4px 0 0;
}
.subtitle {
  text-align: center;
  color: var(--el-text-color-secondary);
  margin: 8px 0 16px;
}
.submit {
  width: 100%;
}
</style>

<script setup>
import { useRouter } from 'vue-router'
import { useAuthStore } from './stores/auth'

const router = useRouter()
const auth = useAuthStore()

function logout() {
  auth.clear()
  router.push('/login')
}
</script>

<template>
  <el-container class="app-shell">
    <el-header class="app-header" height="56px">
      <div class="brand">Text2SQL 分析助手</div>
      <el-menu v-if="auth.isLoggedIn" mode="horizontal" router :default-active="$route.path" class="nav" :ellipsis="false">
        <el-menu-item index="/chat">对话</el-menu-item>
        <el-menu-item index="/history">历史记录</el-menu-item>
        <el-menu-item index="/settings">设置</el-menu-item>
      </el-menu>
      <div v-if="auth.isLoggedIn" class="user-box">
        <span class="username">{{ auth.user?.username }}</span>
        <el-button link type="danger" @click="logout">退出</el-button>
      </div>
    </el-header>
    <el-main class="app-main">
      <router-view />
    </el-main>
  </el-container>
</template>

<style scoped>
.app-shell {
  height: 100vh;
}
.app-header {
  display: flex;
  align-items: center;
  gap: 24px;
  border-bottom: 1px solid var(--el-border-color-light);
}
.brand {
  font-weight: 700;
  font-size: 17px;
  white-space: nowrap;
}
.nav {
  flex: 1;
  border-bottom: none;
}
.user-box {
  display: flex;
  align-items: center;
  gap: 8px;
  white-space: nowrap;
}
.username {
  color: var(--el-text-color-secondary);
}
.app-main {
  padding: 0;
  overflow: hidden;
}
</style>

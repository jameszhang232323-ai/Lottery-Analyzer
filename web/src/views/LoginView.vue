<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import ErrorNote from '../components/ErrorNote.vue'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const username = ref('')
const password = ref('')
const localError = ref('')

const submitting = computed(() => auth.status === 'loading')

function safeRedirect(): string {
  const value = route.query.redirect
  const target = Array.isArray(value) ? value[0] : value
  if (typeof target === 'string' && target.startsWith('/') && !target.startsWith('//')) {
    return target
  }
  return '/'
}

async function submit(): Promise<void> {
  localError.value = ''
  if (!username.value.trim() || !password.value) {
    localError.value = '请输入用户名和密码。'
    return
  }
  const ok = await auth.login(username.value.trim(), password.value)
  if (ok) {
    password.value = ''
    await router.replace(safeRedirect())
  }
}
</script>

<template>
  <main class="login-shell">
    <section class="login-card" aria-labelledby="login-title">
      <div class="login-brand">
        <span class="brand-name">大吉大利</span>
        <span class="brand-sub">福彩数据分析工作台</span>
      </div>
      <h1 id="login-title">登录</h1>
      <p class="login-subtitle">
        使用管理员初始化的账号登录。会话只放在 HttpOnly Cookie 中，页面不保存密码或令牌。
      </p>

      <form class="login-form" @submit.prevent="submit">
        <label class="field">
          <span class="field-label">用户名</span>
          <input
            v-model="username"
            class="field-input"
            type="text"
            name="username"
            autocomplete="username"
            :disabled="submitting"
            required
          />
        </label>
        <label class="field">
          <span class="field-label">密码</span>
          <input
            v-model="password"
            class="field-input"
            type="password"
            name="password"
            autocomplete="current-password"
            :disabled="submitting"
            required
          />
        </label>
        <p v-if="localError" class="error-inline" role="alert">{{ localError }}</p>
        <ErrorNote :error="auth.error" context="登录" />
        <button type="submit" class="action-button login-submit" :disabled="submitting">
          {{ submitting ? '登录中…' : '登录' }}
        </button>
      </form>

      <ul class="plain-list login-notes">
        <li>账号与角色由后端管理；未登录不能访问八个业务页。</li>
        <li>登录失败只显示后端返回的错误类别与错误码，不回显密码。</li>
        <li>连续失败或权限变化以后端会话策略为准。</li>
      </ul>
    </section>
  </main>
</template>

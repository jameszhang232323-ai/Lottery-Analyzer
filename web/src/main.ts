import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import router from './router'
import { useAuthStore } from './stores/auth'
import './styles.css'

const pinia = createPinia()
const app = createApp(App)
app.use(pinia)

// 401 时由会话层统一清空内存并回到登录页；必须在首次导航前注册。
useAuthStore(pinia).installUnauthorizedHandler(router)

app.use(router)
app.mount('#app')

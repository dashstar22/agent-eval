import { createApp } from 'vue'
import { ElButton, ElTag, ElPagination } from 'element-plus'
import 'element-plus/dist/index.css'
import './tokens.css'
import './style.css'
import './workbench.css'
import App from './App.vue'
import router from './router'

createApp(App).use(router).component('ElButton', ElButton).component('ElTag', ElTag)
  .component('ElPagination', ElPagination).mount('#app')

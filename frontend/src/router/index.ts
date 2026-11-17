import { createRouter, createWebHistory } from 'vue-router'

export const navigation = [
  { path: '/dashboard', title: 'Dashboard', caption: '运行概览', symbol: '◫', pending: false },
  { path: '/datasets', title: '数据集', caption: '用例与预期行为', symbol: '▤', pending: false },
  { path: '/agents', title: 'Agent 配置', caption: '连接与版本', symbol: '⌘', pending: false },
  { path: '/experiments', title: '实验', caption: '运行与版本对比', symbol: '◈', pending: false },
  { path: '/evaluations', title: '评测详情', caption: '执行与评分事实', symbol: '≡', pending: false },
  { path: '/badcases', title: 'Badcase', caption: '失败与人工复核', symbol: '⚑', pending: false },
  { path: '/reports', title: '报告', caption: '指标与证据范围', symbol: '▥', pending: false },
]
export default createRouter({ history: createWebHistory(), routes: [
  { path: '/', redirect: '/dashboard' },
  { path: '/dashboard', component: () => import('../views/DashboardView.vue') },
  { path: '/datasets', component: () => import('../views/DatasetsView.vue') },
  { path: '/agents', component: () => import('../views/AgentsView.vue') },
  { path: '/experiments', component: () => import('../views/ExperimentsView.vue') },
  { path: '/evaluations', component: () => import('../views/EvaluationsView.vue') },
  { path: '/badcases', component: () => import('../views/EvaluationsView.vue'), props: { badcases: true } },
  { path: '/reports', component: () => import('../views/ReportsView.vue') },
  { path: '/:pathMatch(.*)*', redirect: '/datasets' },
] })

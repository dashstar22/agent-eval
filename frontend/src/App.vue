<script setup lang="ts">
import { navigation } from './router'
</script>

<template>
  <div class="workspace">
    <a class="skip-link" href="#workspace-content">跳到主要内容</a>
    <aside class="sidebar">
      <RouterLink class="brand" to="/datasets"><span class="brand-symbol">AE</span><span>AgentEval<small>评测工作台</small></span></RouterLink>
      <div class="nav-label">工作空间</div>
      <nav aria-label="主导航">
        <RouterLink v-for="item in navigation" :key="item.path" :to="item.path" class="nav-item"><span class="nav-symbol" aria-hidden="true">{{ item.symbol }}</span><span>{{ item.title }}<small>{{ item.caption }}</small></span><span v-if="item.pending" class="pending-dot" title="待实现"></span></RouterLink>
      </nav>
      <div class="sidebar-footer"><span class="local-dot"></span>本机评测工作区<small>输入、执行事实与评分各自留存</small></div>
    </aside>
    <div class="main-shell">
      <header class="topbar"><span>工作空间 <span class="breadcrumb-slash">/</span> 打印机售后评测</span><span class="topbar-note">个人作品 · 合成用例</span></header>
      <main id="workspace-content" tabindex="-1"><RouterView /></main>
      <footer class="workspace-footer">AgentEval <span>已保存的变更将用于后续运行；历史冻结输入保持原样。</span></footer>
    </div>
  </div>
</template>

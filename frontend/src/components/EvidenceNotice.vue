<script setup lang="ts">
withDefaults(defineProps<{ title: string; description?: string; tone?: 'info' | 'warning' }>(), { tone: 'info' })
</script>

<template>
  <aside class="evidence-notice" :class="`evidence-notice--${tone}`">
    <div><strong>{{ title }}</strong><p v-if="description">{{ description }}</p></div>
    <div v-if="$slots.actions" class="evidence-notice-actions"><slot name="actions" /></div>
    <slot />
  </aside>
</template>

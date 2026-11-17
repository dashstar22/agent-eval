<script setup lang="ts">
defineProps<{ title?: string; description?: string }>()
</script>

<template>
  <section class="panel section-card">
    <div v-if="title || $slots.heading" class="panel-heading">
      <slot name="heading"><div><h2>{{ title }}</h2><p v-if="description" class="small muted">{{ description }}</p></div></slot>
      <slot name="actions" />
    </div>
    <div class="panel-body"><slot /></div>
  </section>
</template>

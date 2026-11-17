<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
defineProps<{ title: string; saving?: boolean; wide?: boolean }>()
const emit = defineEmits<{ close: [] }>()
const container = ref<HTMLElement>()
let previousFocus: HTMLElement | null = null
function keydown(event: KeyboardEvent) {
  if (event.key === 'Escape') emit('close')
  if (event.key !== 'Tab' || !container.value) return
  const fields = [...container.value.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),summary,[tabindex="0"]')].filter(node => node.getClientRects().length)
  const first = fields[0], last = fields[fields.length - 1]
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
onMounted(() => { previousFocus = document.activeElement as HTMLElement; document.body.style.overflow = 'hidden'; container.value?.querySelector<HTMLInputElement>('input:not(:disabled)')?.focus(); document.addEventListener('keydown', keydown) })
onUnmounted(() => { document.body.style.overflow = ''; document.removeEventListener('keydown', keydown); previousFocus?.focus() })
</script>
<template><Teleport to="body"><div class="modal-backdrop"><section ref="container" class="form-modal" :class="{ wide }" role="dialog" aria-modal="true" :aria-label="title"><header class="modal-header"><h2>{{ title }}</h2><button type="button" class="icon-button" aria-label="关闭表单" :disabled="saving" @click="$emit('close')">×</button></header><slot /></section></div></Teleport></template>

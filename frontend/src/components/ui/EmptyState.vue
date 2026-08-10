<template>
  <section class="empty-state" role="status" :aria-label="title">
    <n-icon class="empty-state__icon" size="28" aria-hidden="true">
      <component :is="icon" />
    </n-icon>
    <div class="empty-state__copy">
      <strong>{{ title }}</strong>
      <p>{{ description }}</p>
    </div>
    <div v-if="$slots.actions" class="empty-state__actions">
      <slot name="actions" />
    </div>
  </section>
</template>

<script setup lang="ts">
import type { Component } from 'vue'
import { NIcon } from 'naive-ui'
import { DocumentTextOutline } from '@vicons/ionicons5'

withDefaults(defineProps<{
  title: string
  description: string
  icon?: Component
}>(), {
  icon: () => DocumentTextOutline,
})
</script>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--plotpilot-space-3, 12px);
  min-height: 180px;
  padding: var(--plotpilot-space-6, 24px);
  text-align: center;
  color: var(--app-text-secondary);
  background: var(--app-surface-subtle);
  border: 1px dashed var(--app-border-strong);
  border-radius: var(--app-radius-lg);
}
.empty-state__icon { color: var(--app-text-muted); }
.empty-state__copy strong { color: var(--app-text-primary); font-size: 15px; }
.empty-state__copy p { max-width: 44ch; margin: 6px 0 0; line-height: 1.65; }
.empty-state__actions { margin-top: var(--plotpilot-space-2, 8px); }
</style>

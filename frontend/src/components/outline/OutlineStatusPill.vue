<template>
  <span class="status-pill" :class="`is-${status}`">{{ label }}</span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ status: string }>()

const label = computed(() => ({
  synced: '已同步', syncing: '同步中', published: '已发布', draft: '草稿',
  stale: '已过期', conflict: '有冲突', missing: '未建立',
} as Record<string, string>)[props.status] || props.status)
</script>

<style scoped>
.status-pill { display: inline-flex; align-items: center; padding: 4px 8px; border: 1px solid var(--app-border); border-radius: 999px; color: var(--app-text-secondary); font-size: 11px; font-weight: 700; }
.status-pill.is-synced { color: var(--color-success); border-color: color-mix(in srgb, var(--color-success) 36%, var(--app-border)); }
.status-pill.is-conflict { color: var(--color-danger); }
</style>

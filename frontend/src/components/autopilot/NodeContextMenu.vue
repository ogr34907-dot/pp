<template>
  <Teleport to="body">
    <div
      v-if="visible"
      class="node-context-menu"
      :style="menuStyle"
      @click.stop
    >
      <!-- 节点信息头 -->
      <div class="menu-header">
        <n-text strong style="font-size: 13px">{{ nodeTypeLabel }}</n-text>
      </div>
      <div class="menu-divider" />

      <!-- 只读检查入口 -->
      <div class="menu-item" @click="$emit('detail', nodeId)">
        📋 查看详情
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useDAGStore } from '@/stores/dagStore'
import { CATEGORY_LABELS } from '@/types/dag'

const props = defineProps<{
  x: number
  y: number
  nodeId: string
  nodeType: string
}>()

defineEmits<{
  close: []
  detail: [nodeId: string]
}>()

const dagStore = useDAGStore()

const visible = computed(() => true)

const nodeTypeLabel = computed(() => {
  if (!props.nodeType) return props.nodeId
  const meta = dagStore.nodeTypeRegistry[props.nodeType]
  if (meta) {
    const catLabel = CATEGORY_LABELS[meta.category] || meta.category
    return `${meta.icon} ${meta.display_name} (${catLabel})`
  }
  return props.nodeType
})

// 确保菜单不超出视口
const menuStyle = computed(() => {
  const maxX = window.innerWidth - 200
  const maxY = window.innerHeight - 150
  return {
    left: `${Math.min(props.x, maxX)}px`,
    top: `${Math.min(props.y, maxY)}px`,
  }
})
</script>

<style scoped>
.node-context-menu {
  position: fixed;
  z-index: 9999;
  background: var(--dag-menu-bg);
  border: 1px solid var(--dag-menu-border);
  border-radius: var(--app-radius-sm);
  padding: 4px 0;
  min-width: 160px;
  box-shadow: var(--app-shadow-lg);
  backdrop-filter: blur(8px);
}

.menu-header {
  padding: 8px 16px 4px;
  color: var(--app-text-primary);
}

.menu-item {
  padding: 8px 16px;
  cursor: pointer;
  font-size: var(--font-size-sm);
  color: var(--app-text-primary);
  transition: background 0.15s;
}

.menu-item:hover {
  background: var(--dag-menu-hover);
  color: var(--color-brand);
}

.menu-divider {
  height: 1px;
  background: var(--app-divider);
  margin: 4px 0;
}
</style>

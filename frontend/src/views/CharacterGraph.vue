<template>
  <div class="character-graph-page">
    <header class="graph-header">
      <div class="graph-heading-group">
        <n-button quaternary circle aria-label="返回工作台" @click="handleBack">
          <template #icon><n-icon><ArrowBackOutline /></n-icon></template>
        </n-button>
        <div>
          <p class="graph-eyebrow">关系视图 · {{ novelId }}</p>
          <h1>人物关系图</h1>
        </div>
      </div>
      <n-button secondary @click="handleRefresh" :loading="loading">
        <template #icon><n-icon><RefreshOutline /></n-icon></template>
        刷新
      </n-button>
    </header>

    <div class="graph-container">
      <CharacterRelationGraph
        v-if="novelId"
        :slug="novelId"
        @loading="loading = $event"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NIcon } from 'naive-ui'
import { ArrowBackOutline, RefreshOutline } from '@vicons/ionicons5'
import CharacterRelationGraph from '../components/graphs/CharacterRelationGraph.vue'

const route = useRoute()
const router = useRouter()
const loading = ref(false)

const novelId = computed(() => route.params.slug as string)

const handleBack = () => {
  router.push(`/book/${novelId.value}/workbench`)
}

const handleRefresh = () => {
  window.location.reload()
}
</script>

<style scoped>
.character-graph-page {
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: var(--app-page-bg);
}

.graph-header {
  min-height: 72px;
  padding: 12px clamp(14px, 2vw, 28px);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  background: var(--app-surface);
  border-bottom: 1px solid var(--app-border);
  box-shadow: var(--app-shadow-sm);
  z-index: 2;
}

.graph-heading-group {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.graph-eyebrow {
  margin: 0 0 2px;
  color: var(--app-text-secondary);
  font-size: var(--font-size-xs);
}

.graph-header h1 {
  margin: 0;
  color: var(--app-text-primary);
  font-family: var(--font-serif);
  font-size: 19px;
  font-weight: 600;
}

.graph-container {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  margin: 14px;
  padding: 0;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-md);
  box-shadow: var(--app-shadow-sm);
}

@media (max-width: 640px) {
  .graph-header {
    min-height: 64px;
    padding: 10px 12px;
  }

  .graph-container {
    margin: 8px;
  }
}
</style>

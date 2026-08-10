<template>
  <div class="location-graph-page">
    <header class="graph-header">
      <div class="graph-heading-group">
        <n-button quaternary circle aria-label="返回工作台" @click="handleBack">
          <template #icon><n-icon><ArrowBackOutline /></n-icon></template>
        </n-button>
        <div>
          <p class="graph-eyebrow">空间视图 · {{ novelId }}</p>
          <h1>地点关系图</h1>
        </div>
      </div>
      <n-space>
        <n-button type="primary" @click="openTriplesDrawer()">
          <template #icon><n-icon><GridOutline /></n-icon></template>
          三元组表格
        </n-button>
        <n-button secondary @click="handleRefresh" :loading="loading">
          <template #icon><n-icon><RefreshOutline /></n-icon></template>
          刷新
        </n-button>
      </n-space>
    </header>

    <div class="graph-body">
      <div class="graph-main">
        <LocationRelationGraph
          v-if="novelId"
          ref="locGraphRef"
          :slug="novelId"
          @loading="loading = $event"
          @node-click="handleNodeClick"
        />
      </div>
      <aside class="graph-side">
        <n-tabs v-model:value="activeTab" type="segment" animated>
          <n-tab-pane name="node" tab="地点详情">
            <div v-if="selectedNode" class="side-form">
              <n-button
                block
                type="primary"
                size="small"
                style="margin-bottom: 12px"
                @click="openTriplesDrawer(selectedNode.name)"
              >
                编辑此地点相关三元组
              </n-button>
              <n-descriptions label-placement="left" :column="1" bordered size="small">
                <n-descriptions-item label="名称">{{ selectedNode.name }}</n-descriptions-item>
                <n-descriptions-item label="类型" v-if="selectedNode.location_type">
                  {{ locationTypeLabel(selectedNode.location_type) }}
                </n-descriptions-item>
                <n-descriptions-item label="重要程度" v-if="selectedNode.importance">
                  <n-tag :type="importanceTagType(selectedNode.importance)" size="small">
                    {{ importanceLabel(selectedNode.importance) }}
                  </n-tag>
                </n-descriptions-item>
                <n-descriptions-item label="描述" v-if="selectedNode.description">
                  {{ selectedNode.description }}
                </n-descriptions-item>
                <n-descriptions-item label="首次出现" v-if="selectedNode.first_appearance">
                  第 {{ selectedNode.first_appearance }} 章
                </n-descriptions-item>
                <n-descriptions-item label="相关章节" v-if="selectedNode.related_chapters?.length">
                  <n-space size="small">
                    <n-tag v-for="ch in selectedNode.related_chapters" :key="ch" size="small">
                      第 {{ ch }} 章
                    </n-tag>
                  </n-space>
                </n-descriptions-item>
                <n-descriptions-item label="标签" v-if="selectedNode.tags?.length">
                  <n-space size="small">
                    <n-tag v-for="tag in selectedNode.tags" :key="tag" size="small" type="info">
                      {{ tag }}
                    </n-tag>
                  </n-space>
                </n-descriptions-item>
                <n-descriptions-item label="属性" v-if="selectedNode.attributes && Object.keys(selectedNode.attributes).length">
                  <div class="attributes-list">
                    <div v-for="(value, key) in selectedNode.attributes" :key="key" class="attr-item">
                      <span class="attr-key">{{ key }}:</span>
                      <span class="attr-value">{{ value }}</span>
                    </div>
                  </div>
                </n-descriptions-item>
              </n-descriptions>
            </div>
            <n-empty v-else description="点击图中节点查看地点详情" size="small" style="margin-top: 40px;" />
          </n-tab-pane>
        </n-tabs>
      </aside>
    </div>

    <n-drawer v-model:show="triplesDrawerOpen" :width="920" placement="right" display-directive="if">
      <n-drawer-content title="地点相关三元组" closable>
        <KnowledgeTriplesTableEditor
          v-if="triplesDrawerOpen"
          :key="triplesDrawerKey"
          :slug="novelId"
          default-entity-filter="location"
          :focus-entity-name="triplesDrawerFocus"
          @saved="onTriplesSaved"
        />
      </n-drawer-content>
    </n-drawer>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  NButton,
  NSpace,
  NIcon,
  NTabs,
  NTabPane,
  NDescriptions,
  NDescriptionsItem,
  NTag,
  NEmpty,
  NDrawer,
  NDrawerContent,
} from 'naive-ui'
import { ArrowBackOutline, GridOutline, RefreshOutline } from '@vicons/ionicons5'
import LocationRelationGraph from '../components/graphs/LocationRelationGraph.vue'
import KnowledgeTriplesTableEditor from '../components/knowledge/KnowledgeTriplesTableEditor.vue'
import type { EChartsNode } from '../utils/visToEcharts'
import type { ComponentPublicInstance } from 'vue'
import {
  getLocationImportanceLabel,
  getLocationImportanceTagType,
  getLocationTypeDetailLabel,
} from '@/domain/knowledge'

const route = useRoute()
const router = useRouter()
const loading = ref(false)
const activeTab = ref<'node'>('node')

const locGraphRef = ref<ComponentPublicInstance<{ reload: () => Promise<void> }> | null>(null)
const triplesDrawerOpen = ref(false)
const triplesDrawerFocus = ref('')
const triplesDrawerKey = ref(0)

interface LocationNode extends EChartsNode {
  location_type?: string
  importance?: string
  description?: string
  first_appearance?: number
  related_chapters?: number[]
  tags?: string[]
  attributes?: Record<string, any>
}

const selectedNode = ref<LocationNode | null>(null)

const novelId = computed(() => route.params.slug as string)

const handleBack = () => {
  router.push(`/book/${novelId.value}/workbench`)
}

const handleRefresh = () => {
  window.location.reload()
}

const handleNodeClick = (node: EChartsNode) => {
  selectedNode.value = node as LocationNode
  activeTab.value = 'node'
}

const openTriplesDrawer = (focusName?: string) => {
  triplesDrawerFocus.value = (focusName || '').trim()
  triplesDrawerKey.value += 1
  triplesDrawerOpen.value = true
}

const onTriplesSaved = async () => {
  await locGraphRef.value?.reload?.()
}

const locationTypeLabel = (type: string) => {
  return getLocationTypeDetailLabel(type)
}

const importanceLabel = (importance: string) => {
  return getLocationImportanceLabel(importance)
}

const importanceTagType = (importance: string) => {
  return getLocationImportanceTagType(importance)
}
</script>

<style scoped>
.location-graph-page {
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

.graph-body {
  flex: 1;
  min-height: 0;
  display: flex;
}

.graph-main {
  flex: 1;
  min-width: 0;
  min-height: 0;
  margin: 14px 0 14px 14px;
  overflow: hidden;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-md);
  box-shadow: var(--app-shadow-sm);
}

.graph-side {
  width: min(400px, 42vw);
  flex-shrink: 0;
  padding: 14px;
  overflow: auto;
  background: var(--app-surface);
  border-left: 1px solid var(--app-border);
}

.side-form {
  padding-top: 8px;
}

.attributes-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.attr-item {
  display: flex;
  gap: 8px;
  font-size: 13px;
}

.attr-key {
  font-weight: 500;
  color: var(--app-text-secondary);
}

.attr-value {
  color: var(--app-text-primary);
}

@media (max-width: 1024px) {
  .graph-body {
    flex-direction: column;
    overflow: auto;
  }

  .graph-main {
    flex: none;
    min-height: 58vh;
    margin: 12px;
  }

  .graph-side {
    width: auto;
    overflow: visible;
    border-left: 0;
    border-top: 1px solid var(--app-border);
  }
}

@media (max-width: 640px) {
  .graph-header {
    min-height: 64px;
    padding: 10px 12px;
    align-items: flex-start;
    flex-wrap: wrap;
  }

  .graph-header > :deep(.n-space) {
    width: 100%;
    flex-wrap: wrap !important;
  }

  .graph-main {
    min-height: 52vh;
    margin: 8px;
  }

  .graph-side {
    padding: 12px;
  }
}
</style>

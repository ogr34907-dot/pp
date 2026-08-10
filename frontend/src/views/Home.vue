<template>
  <div class="home" @keydown.esc="mobileSidebarOpen && closeCompactSidebar()">
    <StatsSidebar
      :compact-open="mobileSidebarOpen"
      @create-book="focusCreateInput"
      @refresh-list="handleRefreshList"
      @collapsed-change="handleSidebarCollapsedChange"
      @close-compact="closeCompactSidebar"
    />
    <button
      v-if="mobileSidebarOpen"
      type="button"
      class="compact-sidebar-backdrop"
      aria-label="关闭数据概览面板"
      @click="closeCompactSidebar"
    />
    <main class="home-content" :class="{ 'sidebar-collapsed': sidebarCollapsed }">
      <div class="home-bg" aria-hidden="true" />

      <div class="container">
        <header class="product-header">
          <div class="product-identity">
            <PlotPilotMark size="compact" :label="false" />
            <div>
              <div class="product-name">{{ BRAND.productName }}</div>
              <div class="product-descriptor">{{ BRAND.descriptor }}</div>
            </div>
          </div>
          <n-button
            quaternary
            circle
            size="large"
            class="icon-control"
            aria-label="打开应用设置"
            @click="appSettingsShell.open()"
          >
            <template #icon>
              <n-icon :component="SettingsOutline" :size="21" />
            </template>
          </n-button>
          <n-button
            ref="statsTriggerRef"
            quaternary
            size="large"
            class="mobile-stats-trigger"
            aria-controls="home-stats-sidebar"
            :aria-expanded="mobileSidebarOpen"
            aria-label="打开数据概览与快捷操作"
            @click="mobileSidebarOpen = true"
          >
            <template #icon>
              <n-icon :component="MenuOutline" />
            </template>
            概览
          </n-button>
        </header>

        <section class="library-heading" aria-labelledby="library-title">
          <div class="library-copy">
            <p class="library-kicker">{{ BRAND.tagline }}</p>
            <h1 id="library-title" class="library-title">项目书库</h1>
            <p class="library-subtitle">整理创作项目、查看当前进度，或从一个新梗概开始。</p>
          </div>
          <n-button quaternary size="large" class="library-create-control" @click="focusCreateInput">
            <template #icon>
              <n-icon :component="AddOutline" />
            </template>
            新建书目
          </n-button>
        </section>

        <n-card class="create-card" :bordered="true">
          <n-space vertical :size="20">
            <div class="create-header">
              <div class="create-title-wrap">
                <span class="section-icon" aria-hidden="true">
                  <n-icon :component="CreateOutline" :size="19" />
                </span>
                <div>
                  <h2 class="create-title">创建新书目</h2>
                  <p class="create-hint">先记录故事核心，其余细节可以逐步完善。</p>
                </div>
              </div>
              <n-button text type="primary" @click="showAdvanced = !showAdvanced">
                <template #icon>
                  <n-icon :component="showAdvanced ? ChevronUpOutline : ChevronDownOutline" />
                </template>
                {{ showAdvanced ? '收起高级' : '高级（自定义章数/每章字数）' }}
              </n-button>
            </div>

            <n-input
              ref="createInputRef"
              v-model:value="newBook.premise"
              type="textarea"
              placeholder="用一段话写清主线与爽点预期（不超过 2000 字）…&#10;&#10;例如：废柴赘婿觉醒签到系统，从被退婚到一方巨擘。"
              :rows="5"
              :disabled="creating"
              size="large"
              class="premise-input"
              show-count
              :maxlength="PREMISE_MAX_LEN"
            />

            <div class="taxonomy-block">
              <div class="taxonomy-block-head">
                <span class="taxonomy-block-title">市场分区</span>
                <span class="taxonomy-block-sub">大类 → 细分主题 → 自动写入「类型 / 世界观」；均可再改。</span>
              </div>
              <MarketTaxonomyPicker
                v-model:genre="newBook.genre"
                v-model:worldPreset="newBook.worldPreset"
                v-model:storyStructure="newBook.storyStructure"
                v-model:pacingControl="newBook.pacingControl"
                v-model:writingStyle="newBook.writingStyle"
                v-model:specialRequirements="newBook.specialRequirements"
                :disabled="creating"
              />
            </div>

            <div v-show="!showAdvanced" class="length-tier-block">
              <div class="length-tier-label">目标篇幅（选一个即可，系统按网文常用节奏推导章数）</div>
              <n-radio-group v-model:value="lengthTier" name="lengthTier" class="length-tier-group">
                <n-space :size="14" :wrap="true" align="flex-start" class="length-tier-space">
                  <n-radio
                    v-for="opt in lengthTierOptions"
                    :key="opt.value"
                    :value="opt.value"
                    :disabled="creating"
                    class="length-tier-radio"
                  >
                    <div class="length-tier-option-inner">
                      <span class="length-tier-title">{{ opt.title }}</span>
                      <span class="length-tier-hint">{{ opt.hint }}</span>
                    </div>
                  </n-radio>
                </n-space>
              </n-radio-group>
            </div>

            <div v-show="showAdvanced" class="advanced-settings">
              <n-alert type="info" :show-icon="true" style="margin-bottom: 12px; font-size: 12px">
                自定义章数与每章字数时，不再使用「目标篇幅」档位推导；结构提示仍会在后台写入梗概供模型使用。
              </n-alert>
              <n-grid :cols="2" :x-gap="16" :y-gap="16" responsive="screen">
                <n-gi>
                  <n-form-item label="书名">
                    <n-input v-model:value="newBook.title" placeholder="留空则从梗概自动截取" />
                  </n-form-item>
                </n-gi>
                <n-gi>
                  <n-form-item label="章节数">
                    <n-input-number v-model:value="newBook.chapters" :min="1" :max="9999" class="w-full" placeholder="默认 100 章" />
                  </n-form-item>
                </n-gi>
                <n-gi>
                  <n-form-item label="每章字数">
                    <n-input-number v-model:value="newBook.words" :min="500" :max="20000" :step="500" class="w-full" />
                  </n-form-item>
                </n-gi>
              </n-grid>
            </div>

            <n-space justify="end">
              <n-button
                type="primary"
                size="large"
                round
                :loading="creating"
                :disabled="!newBook.premise.trim() || !newBook.genre.trim() || !newBook.worldPreset.trim() || !newBook.storyStructure.trim() || !newBook.pacingControl.trim() || !newBook.writingStyle.trim() || !newBook.specialRequirements.trim()"
                @click="handleCreate"
              >
                <template #icon>
                  <n-icon :component="SparklesOutline" />
                </template>
                建档并进入工作台
              </n-button>
            </n-space>
          </n-space>
        </n-card>

        <section class="books-section" aria-labelledby="book-list-title">
          <div class="section-header">
            <div class="section-left">
              <h2 id="book-list-title" class="section-title">书目列表</h2>
              <span class="book-count" v-if="!loading">{{ filteredBooks.length }} 本</span>
            </div>
            <div class="section-right">
              <n-input
                v-model:value="searchQuery"
                placeholder="搜索书名或类型…"
                clearable
                round
                class="search-input"
                aria-label="搜索书名或类型"
              >
                <template #prefix>
                  <n-icon :component="SearchOutline" />
                </template>
              </n-input>
              <n-button
                v-if="selectedBooks.length > 0"
                type="error"
                secondary
                @click="showBatchDeleteConfirm = true"
              >
                <template #icon>
                  <n-icon :component="TrashOutline" />
                </template>
                删除选中 ({{ selectedBooks.length }})
              </n-button>
            </div>
          </div>

          <!-- Loading State -->
          <div v-if="loading" class="loading-state">
            <n-spin size="large" />
            <p>加载中…</p>
          </div>

          <!-- Empty State -->
          <div v-else-if="books.length === 0" class="empty-state">
            <div class="empty-illustration">
              <n-icon :component="LibraryOutline" :size="44" aria-hidden="true" />
            </div>
            <h3 class="empty-title">还没有书目</h3>
            <p class="empty-desc">在上方输入你的故事创意，开启创作之旅</p>
            <n-button type="primary" size="large" round @click="focusCreateInput">
              <template #icon>
                <n-icon :component="AddOutline" />
              </template>
              创建第一本书
            </n-button>
          </div>

          <!-- No Results State -->
          <div v-else-if="filteredBooks.length === 0" class="no-results-state">
            <n-icon :component="SearchOutline" :size="36" aria-hidden="true" />
            <p>未找到匹配「{{ searchQuery }}」的书目</p>
            <n-button text type="primary" @click="searchQuery = ''">清除搜索</n-button>
          </div>

          <!-- Books Grid -->
          <template v-else>
            <!-- Selection Bar (仅搜索模式下显示) -->
            <div class="selection-bar" v-if="filteredBooks.length > 0 && searchQuery">
              <n-checkbox
                :checked="isAllSelected"
                :indeterminate="isPartialSelected"
                @update:checked="toggleSelectAll"
              >
                全选
              </n-checkbox>
              <span class="selection-hint" v-if="selectedBooks.length > 0">
                已选择 {{ selectedBooks.length }} 本
              </span>
            </div>

            <!-- 书目卡片：单行横排，多于可视宽度时横向滚动 -->
            <div class="books-list-wrap">
              <div class="books-grid">
                <div
                  v-for="(book, idx) in displayBooks"
                  :key="book.slug"
                  class="book-card"
                  :class="{ 'is-selected': selectedBooks.includes(book.slug) }"
                  :style="{ animationDelay: `${idx * 0.04}s` }"
                  role="link"
                  tabindex="0"
                  :aria-label="`打开书目 ${book.title}`"
                  @click="navigateToBook(book.slug)"
                  @keydown.enter.self="navigateToBook(book.slug)"
                  @keydown.space.self.prevent="navigateToBook(book.slug)"
                >
                  <div class="card-top">
                    <span class="book-dot" :class="`dot-${book.stage}`"></span>
                    <span class="book-card-title">{{ book.title }}</span>
                  </div>
                  <div class="card-meta">
                    <n-tag :type="getStageType(book.stage)" size="small" round borderable>
                      {{ book.stage_label }}
                    </n-tag>
                    <span class="meta-genre">{{ book.genre || '未分类' }}</span>
                  </div>
                  <div class="card-stats" v-if="book.chapter_count || book.word_count">
                    <template v-if="book.chapter_count">
                      <span>{{ book.chapter_count }} 章</span>
                    </template>
                    <template v-if="book.word_count">
                      <span>{{ formatWordCount(book.word_count) }}</span>
                    </template>
                  </div>
                  <div class="card-actions" @click.stop>
                    <n-checkbox
                      :checked="selectedBooks.includes(book.slug)"
                      @update:checked="(val: boolean) => toggleBookSelection(book.slug, val)"
                    />
                    <n-popconfirm
                      positive-text="删除"
                      negative-text="取消"
                      @positive-click="() => handleDeleteBook(book.slug)"
                    >
                      <template #trigger>
                        <n-button
                          quaternary
                          circle
                          size="tiny"
                          type="error"
                          :loading="deletingSlug === book.slug"
                          aria-label="删除书目"
                        >
                          <template #icon>
                            <n-icon :component="TrashOutline" />
                          </template>
                        </n-button>
                      </template>
                      将删除「{{ book.title }}」及本地全部章节与设定，且不可恢复。确定删除吗？
                    </n-popconfirm>
                  </div>
                </div>
              </div>

              <!-- 折叠提示 + 查看全部按钮 -->
              <div v-if="hiddenCount > 0 && !searchQuery" class="books-fold-bar">
                <span class="fold-hint">还有 {{ hiddenCount }} 本书未展示</span>
                <n-button size="small" type="primary" secondary round @click="showAllModal = true">
                  查看全部 {{ filteredBooks.length }} 本
                </n-button>
              </div>
            </div>
          </template>
        </section>

        <footer class="home-ending">书目与创作进度按项目整理。</footer>
      </div>
    </main>

    <!-- Batch Delete Confirm Modal -->
    <n-modal v-model:show="showBatchDeleteConfirm" preset="confirm" type="error" title="确认批量删除">
      <template #default>
        确定要删除选中的 <strong>{{ selectedBooks.length }}</strong> 本书籍吗？此操作不可恢复。
      </template>
      <template #action>
        <n-space>
          <n-button @click="showBatchDeleteConfirm = false">取消</n-button>
          <n-button type="error" :loading="batchDeleting" @click="handleBatchDelete">
            确认删除
          </n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- 新书向导：仅挂载一次且 show 恒为 true，避免「先关再开」的双过渡（原 newNovelId + showSetupGuide 分步更新导致） -->
    <NovelSetupGuide
      v-if="setupWizard"
      :key="setupWizard.novelId"
      :novel-id="setupWizard.novelId"
      :target-chapters="setupWizard.targetChapters"
      :show="true"
      @update:show="(open) => { if (!open) setupWizard = null }"
      @complete="handleSetupComplete"
      @skip="handleSetupSkip"
    />

    <!-- 查看全部书目弹窗 -->
    <n-modal
      v-model:show="showAllModal"
      preset="card"
      title=""
      :style="{ width: '92vw', maxWidth: '960px', height: '80vh', marginTop: '8vh' }"
      :bordered="true"
      :segmented="{ content: true, footer: 'soft' }"
      :mask-closable="true"
      :close-on-esc="true"
    >
      <template #header>
        <div class="all-books-header">
          <span class="all-books-header-title">全部书目</span>
          <n-tag size="small" type="info" :bordered="false">
            {{ filteredBooks.length }} 本
          </n-tag>
        </div>
      </template>

      <div class="all-books-body">
        <n-input
          v-model:value="modalSearchQuery"
          placeholder="搜索书目…"
          clearable
          size="small"
          aria-label="搜索全部书目"
          style="max-width: 280px; margin-bottom: 16px"
        >
          <template #prefix>
            <n-icon :component="SearchOutline" />
          </template>
        </n-input>
        <div class="all-books-grid">
          <div
            v-for="book in modalFilteredBooks"
            :key="book.slug"
            class="book-card"
            role="link"
            tabindex="0"
            :aria-label="`打开书目 ${book.title}`"
            @click="navigateToBook(book.slug); showAllModal = false"
            @keydown.enter.self="navigateToBook(book.slug); showAllModal = false"
            @keydown.space.self.prevent="navigateToBook(book.slug); showAllModal = false"
          >
            <div class="card-top">
              <span class="book-dot" :class="`dot-${book.stage}`"></span>
              <span class="book-card-title">{{ book.title }}</span>
            </div>
            <div class="card-meta">
              <n-tag :type="getStageType(book.stage)" size="small" round borderable>
                {{ book.stage_label }}
              </n-tag>
              <span class="meta-genre">{{ book.genre || '未分类' }}</span>
            </div>
            <div class="card-stats" v-if="book.chapter_count || book.word_count">
              <template v-if="book.chapter_count">
                <span>{{ book.chapter_count }} 章</span>
              </template>
              <template v-if="book.word_count">
                <span>{{ formatWordCount(book.word_count) }}</span>
              </template>
            </div>
            <div class="card-actions" @click.stop>
              <n-popconfirm
                positive-text="删除"
                negative-text="取消"
                @positive-click="() => handleDeleteBook(book.slug)"
              >
                <template #trigger>
                  <n-button
                    quaternary
                    circle
                    size="tiny"
                    type="error"
                    :loading="deletingSlug === book.slug"
                    aria-label="删除书目"
                  >
                    <template #icon>
                      <n-icon :component="TrashOutline" />
                    </template>
                  </n-button>
                </template>
                将删除「{{ book.title }}」及本地全部章节与设定，且不可恢复。确定删除吗？
              </n-popconfirm>
            </div>
          </div>
        </div>
      </div>
    </n-modal>
  </div>
</template>

<script setup lang="ts">
import { defineAsyncComponent, ref, onMounted, computed, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useMessage, NIcon } from 'naive-ui'
import {
  AddOutline,
  ChevronDownOutline,
  ChevronUpOutline,
  CreateOutline,
  LibraryOutline,
  MenuOutline,
  SearchOutline,
  SettingsOutline,
  SparklesOutline,
  TrashOutline,
} from '@vicons/ionicons5'
import { novelApi, type NovelDTO } from '../api/novel'
import { isWizardCompleted } from '@/utils/wizardStageCache'
import PlotPilotMark from '@/components/brand/PlotPilotMark.vue'
import { BRAND } from '@/constants/brand'
import StatsSidebar from '@/components/stats/StatsSidebar.vue'
import { useAppSettingsShellStore } from '@/stores/appSettingsShellStore'
import { parseGenreWorldFromPremise } from '@/utils/premisePresets'
import { useStatsStore } from '@/stores/statsStore'
import { storageKeys } from '@/config/storageKeys'
import { readStorageBoolean } from '@/utils/storage'
import { formatApiError } from '@/utils/apiError'
import {
  NOVEL_LENGTH_TIER_OPTIONS,
  getNovelStageLabel,
  getNovelStageTagType,
  type NovelLengthTier,
} from '@/domain/novel'

const MarketTaxonomyPicker = defineAsyncComponent(
  () => import('@/components/taxonomy/MarketTaxonomyPicker.vue'),
)
const NovelSetupGuide = defineAsyncComponent(
  () => import('@/components/onboarding/NovelSetupGuide.vue'),
)

interface BookListItem {
  slug: string
  title: string
  stage: string
  stage_label: string
  genre: string
  chapter_count?: number
  word_count?: number
}

const router = useRouter()
const message = useMessage()
const statsStore = useStatsStore()
const appSettingsShell = useAppSettingsShellStore()

const createInputRef = ref<any>(null)
const showAdvanced = ref(false)
const creating = ref(false)
const loading = ref(false)

const sidebarCollapsed = ref(readStorageBoolean(storageKeys.statsSidebarCollapsed))
const mobileSidebarOpen = ref(false)
const statsTriggerRef = ref<{ $el?: HTMLElement } | null>(null)

function handleSidebarCollapsedChange(isCollapsed: boolean) {
  sidebarCollapsed.value = isCollapsed
}

function closeCompactSidebar() {
  mobileSidebarOpen.value = false
  nextTick(() => statsTriggerRef.value?.$el?.focus())
}
const books = ref<BookListItem[]>([])
const searchQuery = ref('')
const deletingSlug = ref<string | null>(null)
const showAllModal = ref(false)
const modalSearchQuery = ref('')
/** 有值时挂载向导；与 show 分离，挂载后始终 :show="true"，避免 Modal 先 false 再 true 闪烁 */
const setupWizard = ref<{ novelId: string; targetChapters: number } | null>(null)

// Batch delete
const selectedBooks = ref<string[]>([])
const showBatchDeleteConfirm = ref(false)
const batchDeleting = ref(false)

const PREMISE_MAX_LEN = 2000

const newBook = ref({
  title: '',
  premise: '',
  genre: '',
  worldPreset: '',
  storyStructure: '',
  pacingControl: '',
  writingStyle: '',
  specialRequirements: '',
  chapters: 100,  // 默认 100 章
  words: 2500,
})

/** V1 目标篇幅档（与高级自定义二选一） */
const lengthTier = ref<NovelLengthTier>('standard')
const lengthTierOptions = NOVEL_LENGTH_TIER_OPTIONS

const filteredBooks = computed(() => {
  if (!searchQuery.value.trim()) {
    return books.value
  }
  const query = searchQuery.value.toLowerCase()
  return books.value.filter(
    book =>
      book.title.toLowerCase().includes(query) ||
      (book.genre && book.genre.toLowerCase().includes(query))
  )
})

/** 页面主区域最多展示的书目数量 */
const MAX_VISIBLE_BOOKS = 6

/** 页面实际展示的书目（截断，不滚动） */
const displayBooks = computed(() => {
  if (searchQuery.value.trim()) return filteredBooks.value
  return filteredBooks.value.slice(0, MAX_VISIBLE_BOOKS)
})

/** 被隐藏的数量 */
const hiddenCount = computed(() => {
  if (searchQuery.value.trim()) return 0
  return Math.max(0, filteredBooks.value.length - MAX_VISIBLE_BOOKS)
})

/** 弹窗内的过滤 */
const modalFilteredBooks = computed(() => {
  if (!modalSearchQuery.value.trim()) return filteredBooks.value
  const q = modalSearchQuery.value.toLowerCase()
  return filteredBooks.value.filter(
    book =>
      book.title.toLowerCase().includes(q) ||
      (book.genre && book.genre.toLowerCase().includes(q))
  )
})

const isAllSelected = computed(() => {
  return filteredBooks.value.length > 0 && selectedBooks.value.length === filteredBooks.value.length
})

const isPartialSelected = computed(() => {
  return selectedBooks.value.length > 0 && selectedBooks.value.length < filteredBooks.value.length
})

const fetchBooks = async () => {
  loading.value = true
  try {
    const novels = await novelApi.listNovels()
    books.value = novels.map((novel: NovelDTO) => {
      const fromPrefix = parseGenreWorldFromPremise(novel.premise || '').genre
      const g = novel.locked_genre?.trim() || fromPrefix || ''
      return {
        slug: novel.id,
        title: novel.title,
        stage: novel.stage,
        stage_label: getNovelStageLabel(novel.stage),
        genre: g,
        chapter_count: novel.chapters?.length || 0,
        word_count: novel.total_word_count,
      }
    })
  } catch {
    message.error('加载失败')
  } finally {
    loading.value = false
  }
}

const formatWordCount = (count: number): string => {
  if (count >= 10000) {
    return (count / 10000).toFixed(1) + '万字'
  }
  return count + '字'
}

const handleCreate = async () => {
  if (!newBook.value.premise.trim()) {
    message.warning('请输入核心梗概')
    return
  }
  if (!newBook.value.genre.trim()) {
    message.warning('请在「市场分区」中选定大类与主题')
    return
  }
  if (!newBook.value.worldPreset.trim()) {
    message.warning('请填写或确认世界观基调')
    return
  }
  if (!newBook.value.storyStructure.trim() || !newBook.value.pacingControl.trim() || !newBook.value.writingStyle.trim() || !newBook.value.specialRequirements.trim()) {
    message.warning('请补全四项写作规则')
    return
  }

  creating.value = true
  try {
    const title = newBook.value.title || newBook.value.premise.substring(0, 20)
    const novelId = `novel-${Date.now()}`

    const base = {
      novel_id: novelId,
      title: title,
      author: '作者',
      premise: newBook.value.premise.trim(),
      genre: newBook.value.genre,
      world_preset: newBook.value.worldPreset,
      story_structure: newBook.value.storyStructure,
      pacing_control: newBook.value.pacingControl,
      writing_style: newBook.value.writingStyle,
      special_requirements: newBook.value.specialRequirements,
    }
    const result = await novelApi.createNovel(
      showAdvanced.value
        ? {
            ...base,
            target_chapters: newBook.value.chapters || 100,
            target_words_per_chapter: newBook.value.words || 2500,
          }
        : {
            ...base,
            length_tier: lengthTier.value,
            target_chapters: 0,
          }
    )
    message.success('创建成功')

    setupWizard.value = {
      novelId: result.id,
      targetChapters: result.target_chapters,
    }
  } catch (error: unknown) {
    message.error(formatApiError(error, '创建失败'))
  } finally {
    creating.value = false
  }
}

const handleSetupComplete = () => {
  const id = setupWizard.value?.novelId
  setupWizard.value = null
  if (id) router.push(`/book/${id}/workbench`)
}

const handleSetupSkip = () => {
  const id = setupWizard.value?.novelId
  setupWizard.value = null
  if (id) router.push(`/book/${id}/workbench`)
}

const navigateToBook = (novelId: string) => {
  // 未完成向导的书重新打开向导
  if (!isWizardCompleted(novelId)) {
    // 查找该书的 target_chapters
    const novel = books.value.find(b => b.slug === novelId)
    setupWizard.value = {
      novelId,
      targetChapters: 100, // 默认值，向导内部会从 API 获取真实值
    }
    return
  }
  router.push(`/book/${novelId}/workbench`)
}

const handleDeleteBook = async (slug: string) => {
  deletingSlug.value = slug
  try {
    await novelApi.deleteNovel(slug)
    message.success('书目已删除')
    books.value = books.value.filter(b => b.slug !== slug)
    selectedBooks.value = selectedBooks.value.filter(s => s !== slug)
    await statsStore.loadGlobalStats(true)
  } catch (error: unknown) {
    message.error(formatApiError(error, '删除失败'))
  } finally {
    deletingSlug.value = null
  }
}

const toggleBookSelection = (slug: string, selected: boolean) => {
  if (selected) {
    if (!selectedBooks.value.includes(slug)) {
      selectedBooks.value.push(slug)
    }
  } else {
    selectedBooks.value = selectedBooks.value.filter(s => s !== slug)
  }
}

const toggleSelectAll = (checked: boolean) => {
  if (checked) {
    selectedBooks.value = filteredBooks.value.map(b => b.slug)
  } else {
    selectedBooks.value = []
  }
}

const handleBatchDelete = async () => {
  batchDeleting.value = true
  try {
    let successCount = 0
    let failCount = 0
    
    for (const slug of selectedBooks.value) {
      try {
        await novelApi.deleteNovel(slug)
        successCount++
      } catch {
        failCount++
      }
    }
    
    if (successCount > 0) {
      message.success(`成功删除 ${successCount} 本书目`)
      books.value = books.value.filter(b => !selectedBooks.value.includes(b.slug))
      selectedBooks.value = []
      await statsStore.loadGlobalStats(true)
    }
    if (failCount > 0) {
      message.warning(`${failCount} 本删除失败`)
    }
    showBatchDeleteConfirm.value = false
  } finally {
    batchDeleting.value = false
  }
}

const focusCreateInput = () => {
  nextTick(() => {
    createInputRef.value?.focus()
  })
  // Scroll to top
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

const handleRefreshList = async () => {
  await fetchBooks()
  message.success('列表已刷新')
}

const getStageType = (stage: string) => {
  return getNovelStageTagType(stage)
}

onMounted(() => {
  fetchBooks()
})
</script>

<style scoped>
.home {
  display: flex;
  min-height: 100vh;
  height: 100vh;
  overflow: hidden;
  background: var(--app-page-bg);
}

.home-content {
  flex: 1;
  min-height: 0;
  margin-left: 300px;
  padding: 0 36px 32px;
  position: relative;
  overflow-x: hidden;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  transition: margin-left var(--motion-duration-slow) var(--motion-ease-standard);
}

.home-content.sidebar-collapsed {
  margin-left: 52px;
}

.home-bg {
  position: absolute;
  inset: 0;
  background: var(--app-page-bg);
  z-index: 0;
}

.compact-sidebar-backdrop,
.mobile-stats-trigger {
  display: none;
}

.container {
  position: relative;
  z-index: 1;
  max-width: 1120px;
  margin: 0 auto;
}

.product-header {
  min-height: 68px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  border-bottom: 1px solid var(--app-divider);
}

.product-identity {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  color: var(--color-brand);
}

.product-name {
  color: var(--app-text-primary);
  font-size: 15px;
  font-weight: 750;
  line-height: 1.25;
  letter-spacing: -0.01em;
}

.product-descriptor {
  margin-top: 2px;
  color: var(--app-text-muted);
  font-size: 12px;
  line-height: 1.35;
}

.icon-control {
  color: var(--app-text-secondary);
}

.icon-control:hover {
  color: var(--color-brand);
  background: var(--color-brand-light);
}

.library-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 32px;
  padding: 40px 0 28px;
}

.library-copy {
  min-width: 0;
}

.library-kicker {
  margin: 0 0 6px;
  color: var(--color-brand);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.12em;
}

.library-title {
  margin: 0;
  color: var(--app-text-primary);
  font-family: var(--font-serif);
  font-size: clamp(2rem, 5vw, 3rem);
  font-weight: 700;
  line-height: 1.15;
  letter-spacing: -0.035em;
}

.library-subtitle {
  margin: 10px 0 0;
  max-width: 42rem;
  color: var(--app-text-secondary);
  font-size: 15px;
  line-height: 1.65;
}

.library-create-control {
  min-height: 44px;
  color: var(--color-brand);
  font-weight: 650;
}

.library-create-control:hover {
  background: var(--color-brand-light);
}

.create-card {
  margin-bottom: 24px;
  border-radius: var(--app-radius-lg);
  background: var(--app-surface);
  box-shadow: var(--app-shadow-sm);
}

.create-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.create-title-wrap {
  display: flex;
  align-items: center;
  gap: 12px;
}

.section-icon {
  width: 36px;
  height: 36px;
  display: inline-grid;
  flex: 0 0 auto;
  place-items: center;
  border-radius: var(--app-radius-sm);
  background: var(--color-brand-light);
  color: var(--color-brand);
}

.create-title {
  margin: 0;
  color: var(--app-text-primary);
  font-size: 16px;
  font-weight: 700;
}

.create-hint {
  margin: 3px 0 0;
  color: var(--app-text-muted);
  font-size: 12px;
  line-height: 1.45;
}

.premise-input :deep(textarea) {
  font-size: 15px;
  line-height: 1.6;
}

.taxonomy-block {
  margin-top: 4px;
  padding: 14px 16px;
  border-radius: var(--app-radius-md);
  background: var(--app-surface-subtle);
  border: 1px solid var(--app-border);
}
.taxonomy-block-head {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 12px;
}
.taxonomy-block-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--app-text-primary);
  letter-spacing: 0.04em;
}
.taxonomy-block-sub {
  font-size: 12px;
  color: var(--app-text-muted);
  line-height: 1.45;
}

.length-tier-block {
  margin-top: 8px;
  padding: 4px 0 4px;
}

.length-tier-label {
  font-size: 13px;
  color: var(--app-text-secondary);
  margin-bottom: 10px;
}

.length-tier-space {
  width: 100%;
}

.length-tier-group :deep(.n-radio) {
  align-items: flex-start;
}

.length-tier-radio {
  flex: 1 1 200px;
  min-width: min(200px, 100%);
}

.length-tier-option-inner {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: flex-start;
  max-width: 280px;
}

.length-tier-title {
  font-weight: 600;
  line-height: 1.35;
}

.length-tier-hint {
  font-size: 12px;
  color: var(--app-text-muted);
  line-height: 1.45;
}

.advanced-settings {
  padding: 16px;
  background: var(--color-brand-light);
  border-radius: var(--app-radius-md);
  border: 1px solid var(--color-brand-border);
}

.w-full {
  width: 100%;
}

.books-section {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-lg);
  padding: 24px;
  box-shadow: var(--app-shadow-sm);
}

.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  margin-bottom: 20px;
  flex-wrap: wrap;
}

.section-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.section-title {
  margin: 0;
  font-size: 17px;
  font-weight: 700;
  color: var(--app-text-primary);
}

.book-count {
  font-size: 12px;
  color: var(--app-text-muted);
  background: var(--app-surface-subtle);
  padding: 3px 9px;
  border-radius: 999px;
}

.section-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.search-input {
  width: 248px;
}

.selection-bar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px 16px;
  background: var(--app-surface-subtle);
  border-radius: 10px;
  margin-bottom: 20px;
}

.selection-hint {
  font-size: 13px;
  color: var(--app-text-muted);
}

.loading-state,
.empty-state,
.no-results-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 72px 20px;
  color: var(--app-text-muted);
}

.loading-state p {
  margin-top: 16px;
  font-size: 14px;
}

.empty-state {
  gap: 16px;
}

.empty-illustration {
  width: 88px;
  height: 88px;
  background: var(--app-surface-subtle);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-xl);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-brand);
}

.empty-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: var(--app-text-primary);
}

.empty-desc {
  margin: 0;
  font-size: 14px;
  color: var(--app-text-muted);
}

.no-results-state {
  gap: 12px;
}

.no-results-state p {
  margin: 0;
  font-size: 14px;
}

.books-list-wrap {
  display: flex;
  flex-direction: column;
  gap: 0;
}

.books-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(240px, 100%), 1fr));
  gap: 16px;
}

.book-card {
  position: relative;
  min-width: 0;
  display: flex;
  flex-direction: column;
  padding: 18px;
  background: var(--app-surface-raised);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-md);
  cursor: pointer;
  transition:
    border-color var(--app-transition),
    background var(--app-transition),
    box-shadow var(--app-transition),
    transform var(--app-transition);
  animation: fade-up var(--motion-duration-slow) var(--motion-ease-standard) both;
  overflow: hidden;
}

.book-card:hover {
  border-color: var(--color-brand-border);
  box-shadow: var(--app-shadow-hover);
  transform: translateY(-2px);
}

.book-card:focus-visible {
  outline: none;
  border-color: var(--color-focus);
  box-shadow: var(--focus-ring), var(--app-shadow-md);
}

.book-card.is-selected {
  border-color: var(--color-brand);
  background: var(--color-brand-light);
}

/* 阶段状态小圆点 */
.book-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
  display: inline-block;
}

.book-dot.dot-planning { background: var(--color-info); }
.book-dot.dot-writing { background: var(--color-warning); }
.book-dot.dot-reviewing { background: var(--color-brand); }
.book-dot.dot-completed { background: var(--color-success); }

/* 卡片顶部：标题 + 圆点 */
.card-top {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.book-card-title {
  font-size: 15px;
  font-weight: 650;
  color: var(--app-text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  line-height: 1.3;
}

/* 卡片元信息行：标签 + 类型 */
.card-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}

.meta-genre {
  font-size: 12px;
  color: var(--app-text-muted);
}

/* 卡片统计信息 */
.card-stats {
  display: flex;
  gap: 10px;
  font-size: 12px;
  color: var(--app-text-muted);
  margin-bottom: 12px;
  flex: 1;
}

/* 卡片操作按钮 */
.card-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
  opacity: 0;
  transition: opacity var(--app-transition);
  padding-top: 4px;
  border-top: 1px solid transparent;
}

.book-card:hover .card-actions,
.book-card:focus-within .card-actions {
  opacity: 1;
}

/* 折叠提示栏 */
.books-fold-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 12px;
  padding: 12px 16px;
  background: var(--app-surface-subtle);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-sm);
}

.fold-hint {
  font-size: 13px;
  color: var(--app-text-secondary);
}

@keyframes fade-up {
  from {
    opacity: 0;
    transform: translateY(12px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

/* Responsive */
@media (max-width: 1200px) {
  .home-content {
    padding: 0 24px 28px;
  }
}

.home-ending {
  text-align: center;
  padding: 24px 20px 8px;
  margin-top: 24px;
  border-top: 1px solid var(--app-border);
  font-size: 12px;
  color: var(--app-text-muted);
  line-height: 1.6;
}

@media (max-width: 768px) {
  .compact-sidebar-backdrop {
    position: fixed;
    inset: 0;
    z-index: 190;
    display: block;
    width: 100%;
    height: 100%;
    padding: 0;
    border: 0;
    background: color-mix(in srgb, var(--app-text-primary) 46%, transparent);
    cursor: pointer;
  }

  .mobile-stats-trigger {
    display: inline-flex;
    min-height: 44px;
  }

  .home-content {
    margin-left: 0;
    padding: 0 16px 24px;
  }

  .library-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 20px;
    padding: 32px 0 24px;
  }

  .create-header {
    align-items: flex-start;
    flex-direction: column;
    gap: 12px;
  }

  .section-header {
    flex-direction: column;
    align-items: stretch;
  }
  
  .section-right {
    flex-direction: column;
  }
  
  .search-input {
    width: 100%;
  }

  .card-actions {
    opacity: 1; /* 移动端始终显示操作按钮 */
  }
}

@media (max-width: 480px) {
  .home-content {
    padding-inline: 12px;
  }

  .product-header {
    min-height: 62px;
  }

  .mobile-stats-trigger {
    min-width: 44px;
    padding-inline: 10px;
  }

  .product-descriptor {
    display: none;
  }

  .library-title {
    font-size: 2rem;
  }

  .create-card :deep(.n-card__content),
  .books-section {
    padding: 16px;
  }

  .books-grid {
    grid-template-columns: 1fr;
  }

  .books-fold-bar {
    align-items: flex-start;
    flex-direction: column;
    gap: 10px;
  }
}

/* ── 查看全部书目弹窗样式 ── */
.all-books-header {
  display: flex;
  align-items: center;
  gap: 10px;
}

.all-books-header-title {
  font-size: 17px;
  font-weight: 700;
  color: var(--app-text-primary);
}

.all-books-body {
  height: calc(80vh - 100px);
  overflow-y: auto;
  padding-right: 4px;
}

.all-books-grid {
  max-height: none;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px;
}

@media (prefers-reduced-motion: reduce) {
  .book-card {
    animation: none;
    transition: none;
  }
}
</style>

<template>
  <div class="theme-section">

    <!-- ── 主题模式 ───────────────────────────────── -->
    <div class="section-group">
      <div class="group-header">
        <span class="group-title">配色主题</span>
        <span class="group-hint">立即生效并自动保存</span>
      </div>
      <div class="theme-grid">
        <button
          v-for="option in themeOptions"
          :key="option.value"
          type="button"
          class="theme-tile"
          :class="{ active: themeStore.mode === option.value }"
          :data-mode="option.value"
          :aria-pressed="themeStore.mode === option.value"
          :aria-label="`切换到${option.label}主题`"
          @click="handleThemeChange(option.value)"
        >
          <!-- 缩略预览 -->
          <div class="tile-preview" :class="option.previewClass">
            <div class="tile-preview-bar">
              <span class="tile-dot"></span>
              <span class="tile-dot"></span>
              <span class="tile-dot"></span>
            </div>
            <div class="tile-preview-lines">
              <div class="tile-line w-full"></div>
              <div class="tile-line w-3/4"></div>
              <div class="tile-line w-1/2"></div>
            </div>
          </div>
          <div class="tile-meta">
            <n-icon class="tile-icon" :component="option.icon" aria-hidden="true" />
            <span class="tile-name">{{ option.label }}</span>
            <n-icon
              v-if="themeStore.mode === option.value"
              class="tile-check"
              :component="CheckmarkCircle"
              aria-hidden="true"
            />
          </div>
        </button>
      </div>
    </div>

    <!-- ── 界面字号 ────────────────────────────────── -->
    <div class="section-group">
      <div class="group-header">
        <span class="group-title">界面字号</span>
        <span class="group-hint">悬停预览 · 点击保存</span>
      </div>

      <div class="size-layout">
        <!-- 4 个字号卡 -->
        <div class="size-cards">
          <button
            v-for="opt in fontSizeOptions"
            :key="opt.value"
            type="button"
            class="size-card"
            :class="{ active: fontSizeStore.preset === opt.value, hovering: hoverPreset === opt.value }"
            @mouseenter="hoverPreset = opt.value"
            @mouseleave="hoverPreset = null"
            @click="handleFontSizeChange(opt.value)"
          >
            <span class="size-aa" :style="{ fontSize: opt.aaPx }">Aa</span>
            <span class="size-name">{{ opt.label }}</span>
            <span class="size-pct">{{ opt.hint }}</span>
          </button>
        </div>

        <!-- 实时预览区：字号随悬停/选中变化 -->
        <div class="size-preview-box" :style="previewBoxStyle">
          <div class="preview-chapter-label">第十二章 · 目标 {{ previewWordCount }} 字</div>
          <p class="preview-body-text">
            这是一段示例正文，展示当前字号下的阅读体验。全托管节拍续写已完成
            <strong>{{ previewWordCountDone }}</strong> 字，排版与行距随界面档位同步缩放。
          </p>
          <div class="preview-status-row">
            <span class="preview-progress-bar">
              <span class="preview-progress-fill" :style="{ width: '68%' }"></span>
            </span>
            <span class="preview-pct-label">68%</span>
          </div>
        </div>
      </div>
    </div>

  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { NIcon, useMessage } from 'naive-ui'
import { CheckmarkCircle, MoonOutline, SunnyOutline } from '@vicons/ionicons5'
import { useThemeStore, type ThemeMode } from '@/stores/themeStore'
import { useFontSizeStore, type FontSizePreset } from '@/stores/fontSizeStore'

const message = useMessage()
const themeStore = useThemeStore()
const fontSizeStore = useFontSizeStore()

const hoverPreset = ref<FontSizePreset | null>(null)

const SCALE_MAP: Record<FontSizePreset, number> = {
  small: 0.875,
  medium: 1,
  large: 1.125,
  xlarge: 1.25,
}

const fontSizeOptions: { value: FontSizePreset; label: string; hint: string; aaPx: string }[] = [
  { value: 'small',  label: '较小', hint: '87.5%', aaPx: '18px' },
  { value: 'medium', label: '默认', hint: '100%',  aaPx: '22px' },
  { value: 'large',  label: '较大', hint: '112.5%',aaPx: '26px' },
  { value: 'xlarge', label: '特大', hint: '125%',  aaPx: '30px' },
]

const effectivePreset = computed<FontSizePreset>(() => hoverPreset.value ?? fontSizeStore.preset)

const previewBoxStyle = computed(() => {
  const scale = SCALE_MAP[effectivePreset.value]
  return { fontSize: `${scale * 14}px` }
})

const previewWordCount = computed(() => {
  const scale = SCALE_MAP[effectivePreset.value]
  return scale >= 1.2 ? '1,500' : scale >= 1.1 ? '1,800' : '2,000'
})
const previewWordCountDone = computed(() => {
  const scale = SCALE_MAP[effectivePreset.value]
  return scale >= 1.2 ? '1,020' : scale >= 1.1 ? '1,224' : '1,360'
})

const themeOptions = [
  {
    value: 'light' as ThemeMode,
    label: '浅色',
    previewClass: 'prev-light',
    icon: SunnyOutline,
  },
  {
    value: 'dark' as ThemeMode,
    label: '深色',
    previewClass: 'prev-dark',
    icon: MoonOutline,
  },
]

function handleFontSizeChange(next: FontSizePreset) {
  if (fontSizeStore.preset === next) return
  fontSizeStore.setPreset(next)
  const label = fontSizeOptions.find((o) => o.value === next)?.label ?? next
  message.success(`字号已设为「${label}」`)
}

function handleThemeChange(newMode: ThemeMode) {
  const opt = themeOptions.find((o) => o.value === newMode)
  const label = opt?.label ?? newMode
  const applyTheme = () => { themeStore.setTheme(newMode) }
  if ('startViewTransition' in document) {
    ;(document as Document & { startViewTransition: (cb: () => void) => void })
      .startViewTransition(applyTheme)
  } else {
    const root = (document as Document).documentElement as HTMLElement
    root.classList.add('theme-transitioning')
    applyTheme()
    setTimeout(() => root.classList.remove('theme-transitioning'), 220)
  }
  message.success(`已切换到${label}主题`)
}
</script>

<style scoped>
.theme-section {
  display: flex;
  flex-direction: column;
  gap: 1.75rem;
}

/* ── 分组 ── */
.section-group {
  display: flex;
  flex-direction: column;
  gap: 0.875rem;
}

.group-header {
  display: flex;
  align-items: baseline;
  gap: 0.625rem;
}

.group-title {
  font-size: var(--font-size-sm);
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--app-text-muted);
}

.group-hint {
  font-size: calc(var(--font-size-xs) * 0.96);
  color: var(--app-text-muted);
  opacity: 0.7;
}

/* ── 主题选择 ── */
.theme-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: var(--plotpilot-space-3, 12px);
}

.theme-tile {
  border-radius: 0.875rem;
  border: 1.5px solid var(--app-border, #e2e8f0);
  overflow: hidden;
  cursor: pointer;
  color: inherit;
  text-align: left;
  appearance: none;
  transition:
    border-color var(--motion-duration-standard) var(--motion-ease-standard),
    box-shadow var(--motion-duration-standard) var(--motion-ease-standard),
    transform var(--motion-duration-standard) var(--motion-ease-standard);
  background: var(--app-surface);
}

.theme-tile:hover {
  border-color: var(--color-brand-border);
  box-shadow: var(--app-shadow-md);
  transform: translateY(-1px);
}

.theme-tile.active {
  border-color: var(--color-brand);
  box-shadow: var(--focus-ring), var(--app-shadow-sm);
}

/* 缩略预览 */
.tile-preview {
  height: 4rem;
  padding: 0.5rem 0.625rem;
  display: flex;
  flex-direction: column;
  gap: 0.375rem;
  transition: background var(--motion-duration-slow) var(--motion-ease-standard);
}

.prev-light { background: #F7F5EF; }
.prev-dark { background: #141A18; }

.tile-preview-bar {
  display: flex;
  gap: 0.25rem;
}

.tile-dot {
  width: 0.375rem;
  height: 0.375rem;
  border-radius: 50%;
}

.prev-light .tile-dot { background: #D7DDD6; }
.prev-dark .tile-dot { background: #33413C; }

.tile-preview-lines {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.tile-line {
  height: 0.3rem;
  border-radius: 0.2rem;
}

.w-full { width: 100%; }
.w-3\/4  { width: 75%; }
.w-1\/2  { width: 50%; }

.prev-light .tile-line { background: #D7DDD6; }
.prev-dark .tile-line { background: #25312D; }

/* 底部标签区 */
.tile-meta {
  display: flex;
  align-items: center;
  gap: 0.375rem;
  padding: 0.44rem 0.625rem 0.56rem;
  border-top: 1px solid var(--app-border, #e2e8f0);
}

.tile-icon {
  display: flex;
  align-items: center;
  flex-shrink: 0;
  color: var(--color-brand);
}

.tile-name {
  flex: 1;
  font-size: var(--font-size-xs);
  font-weight: 600;
  color: var(--app-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.tile-check {
  flex-shrink: 0;
  color: var(--color-brand, #2563eb);
}

/* ── 字号卡 + 预览 ── */
.size-layout {
  display: flex;
  flex-direction: column;
  gap: 0.875rem;
}

.size-cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 0.5rem;
}

.size-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.25rem;
  padding: 0.875rem 0.625rem 0.75rem;
  border-radius: 0.8125rem;
  border: 1.5px solid var(--app-border, #e2e8f0);
  background: var(--app-surface);
  cursor: pointer;
  text-align: center;
  transition:
    border-color var(--motion-duration-fast) var(--motion-ease-standard),
    box-shadow var(--motion-duration-fast) var(--motion-ease-standard),
    transform var(--motion-duration-fast) var(--motion-ease-standard);
}

.size-card:hover,
.size-card.hovering {
  border-color: var(--color-brand-border);
  box-shadow: var(--app-shadow-sm);
  transform: translateY(-1px);
}

.size-card.active {
  border-color: var(--color-brand, #2563eb);
  background: var(--color-brand-light);
  box-shadow: var(--focus-ring), var(--app-shadow-sm);
}

.size-aa {
  font-weight: 700;
  line-height: 1;
  color: var(--app-text-primary);
  letter-spacing: -0.02em;
  transition: font-size var(--motion-duration-fast) var(--motion-ease-standard);
}

.size-name {
  font-size: var(--font-size-xs);
  font-weight: 600;
  color: var(--app-text-secondary);
}

.size-pct {
  font-size: calc(var(--font-size-xs) * 0.88);
  color: var(--app-text-muted);
}

/* 实时预览框 */
.size-preview-box {
  border-radius: 0.75rem;
  border: 1px solid var(--app-border, #e2e8f0);
  background: var(--app-surface-subtle, #f8fafc);
  padding: 0.875rem 1rem;
  transition: font-size var(--motion-duration-standard) var(--motion-ease-standard);
  user-select: none;
}

.preview-chapter-label {
  font-size: 0.75em;
  font-weight: 700;
  color: var(--app-text-muted);
  letter-spacing: 0.03em;
  margin-bottom: 0.375rem;
}

.preview-body-text {
  line-height: 1.7;
  color: var(--app-text-secondary);
  margin-bottom: 0.625rem;
}

.preview-status-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.preview-progress-bar {
  flex: 1;
  height: 0.25rem;
  border-radius: 0.125rem;
  background: var(--app-border, #e2e8f0);
  overflow: hidden;
  display: block;
}

.preview-progress-fill {
  display: block;
  height: 100%;
  border-radius: 0.125rem;
  background: var(--color-brand, #2563eb);
  transition: width var(--motion-duration-slow) var(--motion-ease-standard);
}

.preview-pct-label {
  font-size: 0.75em;
  font-weight: 600;
  color: var(--app-text-muted);
  white-space: nowrap;
}
</style>

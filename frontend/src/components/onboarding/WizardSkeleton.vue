<!-- 书设置向导骨架屏 - 在LLM生成数据期间显示 -->
<template>
  <div class="wizard-skeleton">
    <!-- 世界观维度骨架 -->
    <template v-if="type === 'worldbuilding'">
      <div class="worldbuilding-progress">
        <aside class="worldbuilding-progress__rail" aria-label="世界观生成阶段">
          <div class="worldbuilding-progress__rail-head">
            <span class="worldbuilding-progress__eyebrow">生成阶段</span>
            <span class="worldbuilding-progress__count">
              {{ invocationActive ? '完整设定生成中' : `${completedCount} / ${dimensions.length}` }}
            </span>
          </div>
          <div class="worldbuilding-progress__list" role="list">
            <button
              v-for="dim in dimensions"
              :key="dim.key"
              type="button"
              class="worldbuilding-progress__item"
              :class="{
                'worldbuilding-progress__item--active': !invocationActive && activeDimension === dim.key && !completedDimensions.has(dim.key),
                'worldbuilding-progress__item--done': !invocationActive && completedDimensions.has(dim.key),
                'worldbuilding-progress__item--selected': selectedDimension.key === dim.key,
              }"
              role="listitem"
              @click="previewDimension = dim.key"
            >
              <span
                class="skeleton-dot"
                :class="{
                  'skeleton-dot--active': !invocationActive && activeDimension === dim.key && !completedDimensions.has(dim.key),
                  'skeleton-dot--done': !invocationActive && completedDimensions.has(dim.key),
                }"
                aria-hidden="true"
              >
                <span v-if="!invocationActive && completedDimensions.has(dim.key)" class="skeleton-dot__check">✓</span>
                <span v-else-if="!invocationActive && activeDimension === dim.key" class="skeleton-dot__pulse"></span>
              </span>
              <span class="worldbuilding-progress__item-text">
                <span class="worldbuilding-progress__item-title">{{ dim.label }}</span>
                <span class="worldbuilding-progress__item-status">{{ dimensionStatus(dim.key) }}</span>
              </span>
              <span class="worldbuilding-progress__item-arrow" aria-hidden="true">›</span>
            </button>
          </div>
          <div class="worldbuilding-progress__rail-foot">
            <span class="worldbuilding-progress__legend-dot worldbuilding-progress__legend-dot--active" />
            <span>{{ invocationActive ? '完整结果校验后将写入五个维度' : '当前生成项可实时查看' }}</span>
          </div>
        </aside>

        <section class="worldbuilding-progress__preview" aria-live="polite">
          <header class="worldbuilding-progress__preview-head">
            <div>
              <span class="worldbuilding-progress__eyebrow">实时预览</span>
              <h4>{{ invocationActive ? '完整世界观设定' : selectedDimension.label }}</h4>
            </div>
            <span class="worldbuilding-progress__state" :class="`worldbuilding-progress__state--${selectedStatus.type}`">
              {{ selectedStatus.label }}
            </span>
          </header>

          <div
            v-if="invocationActive || (activeDimension === selectedDimension.key && !completedDimensions.has(selectedDimension.key))"
            class="worldbuilding-progress__live-line"
          >
            <span class="worldbuilding-progress__live-dot" aria-hidden="true" />
            <span>{{ invocationActive ? invocationMessage : (phaseMessage || `正在生成${selectedDimension.label}`) }}</span>
          </div>

          <div
            v-if="!invocationActive && (activeDimension === selectedDimension.key || completedDimensions.has(selectedDimension.key))"
            class="worldbuilding-progress__preview-body"
          >
            <slot :name="selectedDimension.key" />
          </div>
          <div v-else-if="invocationActive" class="worldbuilding-progress__invocation-copy">
            <p>{{ invocationMessage || '正在等待服务端返回完整设定。' }}</p>
          </div>
          <div v-else class="worldbuilding-progress__waiting">
            <div class="worldbuilding-progress__waiting-lines" aria-hidden="true">
              <span />
              <span />
              <span />
            </div>
            <p>{{ phaseMessage || '等待前一阶段完成后开始生成' }}</p>
          </div>
        </section>
      </div>
    </template>

    <!-- 人物骨架 -->
    <template v-else-if="type === 'characters'">
      <div class="skeleton-characters">
        <div
          v-for="i in 3"
          :key="i"
          class="skeleton-character"
          :class="{ 'skeleton-character--done': i <= completedCount }"
        >
          <div class="skeleton-character__avatar">
            <span v-if="i <= completedCount" class="skeleton-dot__check">✓</span>
            <span v-else class="skeleton-dot__pulse"></span>
          </div>
          <div class="skeleton-character__info">
            <div class="skeleton-bar skeleton-bar--name" :class="{ 'skeleton-bar--shimmer': i > completedCount }"></div>
            <div class="skeleton-bar skeleton-bar--desc" :class="{ 'skeleton-bar--shimmer': i > completedCount }"></div>
          </div>
        </div>
      </div>
    </template>

    <!-- 地图骨架 -->
    <template v-else-if="type === 'locations'">
      <div class="skeleton-locations">
        <div class="skeleton-map">
          <div class="skeleton-map__placeholder">
            <div class="skeleton-dot__pulse skeleton-map__pulse"></div>
            <span>地图生成中...</span>
          </div>
        </div>
        <div class="skeleton-locations__list">
          <div
            v-for="i in 4"
            :key="i"
            class="skeleton-location"
            :class="{ 'skeleton-location--done': i <= completedCount }"
          >
            <div class="skeleton-bar skeleton-bar--loc-name" :class="{ 'skeleton-bar--shimmer': i > completedCount }"></div>
            <div class="skeleton-bar skeleton-bar--loc-desc" :class="{ 'skeleton-bar--shimmer': i > completedCount }"></div>
          </div>
        </div>
      </div>
    </template>

    <!-- 故事线骨架 -->
    <template v-else-if="type === 'storyline'">
      <div class="skeleton-storyline">
        <div v-for="i in 3" :key="i" class="skeleton-storyline__card">
          <div class="skeleton-bar skeleton-bar--plot-title"></div>
          <div class="skeleton-bar skeleton-bar--plot-line"></div>
          <div class="skeleton-bar skeleton-bar--plot-line skeleton-bar--short"></div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 骨架屏类型 */
    type: 'worldbuilding' | 'characters' | 'locations' | 'storyline'
    /** 世界观：当前正在生成的维度 key */
    activeDimension?: string
    /** 世界观：已完成的维度 key 集合 */
    completedDimensions?: Set<string>
    /** 人物/地点：已完成的数量 */
    completedCount?: number
    /** 世界观当前阶段的状态描述 */
    phaseMessage?: string
    /** 一次性 Invocation 会在校验后统一写入，不可伪装成逐维度完成。 */
    invocationActive?: boolean
    invocationMessage?: string
  }>(),
  {
    activeDimension: '',
    completedDimensions: () => new Set<string>(),
    completedCount: 0,
    phaseMessage: '',
    invocationActive: false,
    invocationMessage: '',
  }
)

const dimensions = [
  { key: 'core_rules', label: '核心法则' },
  { key: 'geography', label: '地理生态' },
  { key: 'society', label: '社会结构' },
  { key: 'culture', label: '历史文化' },
  { key: 'daily_life', label: '沉浸感细节' },
]

const previewDimension = ref(props.activeDimension || dimensions[0].key)

const selectedDimension = computed(() =>
  dimensions.find(dim => dim.key === previewDimension.value) || dimensions[0],
)

const completedCount = computed(() => dimensions.filter(dim => props.completedDimensions.has(dim.key)).length)

const selectedStatus = computed(() => {
  if (props.invocationActive) {
    return { label: '统一生成中', type: 'info' as const }
  }
  if (props.completedDimensions.has(selectedDimension.value.key)) {
    return { label: '已生成', type: 'success' as const }
  }
  if (props.activeDimension === selectedDimension.value.key) {
    return { label: '生成中', type: 'info' as const }
  }
  return { label: '等待中', type: 'default' as const }
})

function dimensionStatus(key: string) {
  if (props.invocationActive) return '统一生成中'
  if (props.completedDimensions.has(key)) return '已生成'
  if (props.activeDimension === key) return '生成中'
  return '等待中'
}

watch(
  () => props.activeDimension,
  (next) => {
    if (next && dimensions.some(dim => dim.key === next)) {
      previewDimension.value = next
    }
  },
)
</script>

<style scoped>
.wizard-skeleton {
  width: 100%;
}

.worldbuilding-progress {
  display: grid;
  grid-template-columns: minmax(190px, 0.34fr) minmax(0, 1fr);
  gap: 14px;
  min-height: 264px;
}

.worldbuilding-progress__rail,
.worldbuilding-progress__preview {
  min-width: 0;
  border: 1px solid var(--app-border, var(--n-border-color));
  border-radius: 8px;
  background: var(--app-surface, var(--n-color-modal));
}

.worldbuilding-progress__rail {
  display: flex;
  flex-direction: column;
  padding: 12px;
}

.worldbuilding-progress__rail-head,
.worldbuilding-progress__preview-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.worldbuilding-progress__rail-head {
  padding: 0 2px 10px;
  border-bottom: 1px solid var(--app-border, var(--n-border-color));
}

.worldbuilding-progress__eyebrow {
  color: var(--app-text-muted, var(--n-text-color-3));
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.worldbuilding-progress__count {
  color: var(--app-text-secondary, var(--n-text-color-2));
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.worldbuilding-progress__list {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 4px;
  padding-top: 8px;
}

.worldbuilding-progress__item {
  display: grid;
  grid-template-columns: 18px minmax(0, 1fr) auto;
  align-items: center;
  gap: 9px;
  width: 100%;
  min-height: 44px;
  padding: 6px 8px;
  border: 1px solid transparent;
  border-radius: 6px;
  color: var(--app-text-primary, var(--n-text-color-1));
  background: transparent;
  text-align: left;
  cursor: pointer;
  transition: border-color 0.2s ease, background 0.2s ease, color 0.2s ease;
}

.worldbuilding-progress__item:hover,
.worldbuilding-progress__item:focus-visible {
  border-color: var(--app-border-strong, var(--n-border-color));
  background: var(--app-surface-subtle, var(--n-color-modal));
  outline: none;
}

.worldbuilding-progress__item--selected {
  border-color: color-mix(in srgb, var(--color-brand, var(--n-primary-color)) 28%, var(--app-border));
  background: color-mix(in srgb, var(--color-brand, var(--n-primary-color)) 7%, var(--app-surface));
}

.worldbuilding-progress__item--done {
  color: var(--app-text-secondary, var(--n-text-color-2));
}

.worldbuilding-progress__item-text {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
}

.worldbuilding-progress__item-title {
  overflow: hidden;
  font-size: 13px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.worldbuilding-progress__item-status {
  color: var(--app-text-muted, var(--n-text-color-3));
  font-size: 11px;
}

.worldbuilding-progress__item--active .worldbuilding-progress__item-status {
  color: var(--color-brand, var(--n-primary-color));
}

.worldbuilding-progress__item--done .worldbuilding-progress__item-status {
  color: var(--color-success, var(--n-success-color));
}

.worldbuilding-progress__item-arrow {
  color: var(--app-text-muted, var(--n-text-color-3));
  font-size: 18px;
  line-height: 1;
}

.worldbuilding-progress__rail-foot {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 2px 0;
  border-top: 1px solid var(--app-border, var(--n-border-color));
  color: var(--app-text-muted, var(--n-text-color-3));
  font-size: 11px;
  line-height: 1.45;
}

.worldbuilding-progress__legend-dot,
.worldbuilding-progress__live-dot {
  display: inline-block;
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--color-brand, var(--n-primary-color));
}

.worldbuilding-progress__legend-dot--active,
.worldbuilding-progress__live-dot {
  animation: pulse-glow 1.4s ease-in-out infinite;
}

.worldbuilding-progress__preview {
  display: flex;
  flex-direction: column;
  padding: 16px;
}

.worldbuilding-progress__preview-head {
  align-items: flex-start;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--app-border, var(--n-border-color));
}

.worldbuilding-progress__preview-head h4 {
  margin: 5px 0 0;
  color: var(--app-text-primary, var(--n-text-color-1));
  font-size: 17px;
  font-weight: 700;
}

.worldbuilding-progress__state {
  flex: none;
  padding: 3px 8px;
  border: 1px solid var(--app-border, var(--n-border-color));
  border-radius: 999px;
  color: var(--app-text-secondary, var(--n-text-color-2));
  background: var(--app-surface-subtle, var(--n-color-modal));
  font-size: 12px;
}

.worldbuilding-progress__state--info {
  color: var(--color-brand, var(--n-primary-color));
  border-color: color-mix(in srgb, var(--color-brand, var(--n-primary-color)) 34%, var(--app-border));
}

.worldbuilding-progress__live-line {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 12px 0 2px;
  color: var(--app-text-secondary, var(--n-text-color-2));
  font-size: 12px;
  line-height: 1.5;
}

.worldbuilding-progress__preview-body {
  min-height: 0;
  padding-top: 12px;
  overflow: auto;
}

.worldbuilding-progress__invocation-copy {
  display: flex;
  flex: 1;
  align-items: center;
  min-height: 112px;
  color: var(--app-text-secondary, var(--n-text-color-2));
  font-size: 13px;
  line-height: 1.65;
}

.worldbuilding-progress__invocation-copy p { margin: 0; }

.worldbuilding-progress__waiting {
  display: flex;
  flex: 1;
  flex-direction: column;
  justify-content: center;
  min-height: 170px;
  color: var(--app-text-muted, var(--n-text-color-3));
  text-align: center;
}

.worldbuilding-progress__waiting p {
  margin: 12px 0 0;
  font-size: 12px;
}

.worldbuilding-progress__waiting-lines {
  display: flex;
  flex-direction: column;
  gap: 9px;
  width: min(88%, 420px);
  margin: 0 auto;
}

.worldbuilding-progress__waiting-lines span {
  display: block;
  height: 12px;
  border-radius: 4px;
  background: linear-gradient(90deg, var(--app-surface-subtle) 25%, var(--app-border) 50%, var(--app-surface-subtle) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s ease-in-out infinite;
}

.worldbuilding-progress__waiting-lines span:nth-child(2) { width: 82%; }
.worldbuilding-progress__waiting-lines span:nth-child(3) { width: 58%; }

/* 世界观维度 */
.skeleton-dimension {
  padding: 12px 16px;
  border-radius: 8px;
  margin-bottom: 8px;
  background: var(--app-surface, var(--n-color-modal));
  border: 1px solid var(--app-border, var(--n-border-color));
  transition: border-color 0.3s ease, background 0.3s ease, box-shadow 0.3s ease;
}

.skeleton-dimension--done {
  border-color: color-mix(in srgb, var(--color-success, #22c55e) 34%, var(--app-border, transparent));
  background:
    linear-gradient(90deg, color-mix(in srgb, var(--color-success, #22c55e) 7%, transparent), transparent 48%),
    var(--app-surface, var(--n-color-modal));
}

.skeleton-dimension--active {
  border-color: color-mix(in srgb, var(--color-brand, #2563eb) 42%, var(--app-border, transparent));
  background:
    linear-gradient(90deg, color-mix(in srgb, var(--color-brand, #2563eb) 9%, transparent), transparent 52%),
    var(--app-surface, var(--n-color-modal));
  box-shadow: 0 8px 22px color-mix(in srgb, var(--color-brand, #2563eb) 8%, transparent);
}

.skeleton-dimension__header {
  display: flex;
  align-items: center;
  gap: 10px;
}

.skeleton-dimension__title {
  font-weight: 500;
  font-size: 14px;
  color: var(--app-text-primary, var(--n-text-color-1));
  flex: 1;
}

.skeleton-dimension__body {
  margin-top: 8px;
  padding-left: 26px;
}

.skeleton-dimension__content {
  margin-top: 8px;
  padding-left: 26px;
}

/* 圆点指示器 */
.skeleton-dot {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  border: 2px solid var(--app-border-strong, var(--n-border-color));
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: all 0.3s ease;
}

.skeleton-dot--active {
  border-color: var(--color-brand, var(--n-primary-color));
  background: var(--color-brand-light, rgba(37, 99, 235, 0.08));
}

.skeleton-dot--done {
  border-color: var(--color-success, var(--n-success-color));
  background: var(--color-success, var(--n-success-color));
}

.skeleton-dot__check {
  color: white;
  font-size: 10px;
  font-weight: bold;
}

.skeleton-dot__pulse {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--color-brand, var(--n-primary-color));
  animation: pulse-glow 1.2s ease-in-out infinite;
}

@keyframes pulse-glow {
  0%, 100% { opacity: 0.4; transform: scale(0.8); }
  50% { opacity: 1; transform: scale(1.2); }
}

/* 骨架条 */
.skeleton-bar {
  height: 14px;
  border-radius: 4px;
  margin-bottom: 8px;
  background: linear-gradient(90deg, #f0f0f0 25%, #e0e0e0 50%, #f0f0f0 75%);
  background-size: 200% 100%;
}

.skeleton-bar--shimmer {
  animation: shimmer 1.5s ease-in-out infinite;
}

@keyframes shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

.skeleton-bar--long { width: 90%; }
.skeleton-bar--medium { width: 70%; }
.skeleton-bar--short { width: 50%; }
.skeleton-bar--name { width: 40%; height: 16px; }
.skeleton-bar--desc { width: 80%; height: 12px; margin-top: 6px; }
.skeleton-bar--loc-name { width: 35%; height: 14px; }
.skeleton-bar--loc-desc { width: 75%; height: 12px; margin-top: 4px; }
.skeleton-bar--plot-title { width: 50%; height: 18px; margin-bottom: 10px; }
.skeleton-bar--plot-line { width: 85%; height: 12px; margin-bottom: 6px; }

/* 人物骨架 */
.skeleton-characters {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.skeleton-character {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px;
  border-radius: 8px;
  border: 1px solid var(--n-border-color);
  background: var(--n-color-modal);
  transition: all 0.3s ease;
}

.skeleton-character--done {
  border-color: #18a05840;
  background: #18a05808;
}

.skeleton-character__avatar {
  width: 40px;
  height: 40px;
  border-radius: 50%;
  background: #f0f0f0;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.skeleton-character__info {
  flex: 1;
}

/* 地图骨架 */
.skeleton-map {
  height: 200px;
  border-radius: 8px;
  border: 1px solid var(--n-border-color);
  background: var(--n-color-modal);
  margin-bottom: 12px;
  overflow: hidden;
}

.skeleton-map__placeholder {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: #999;
  font-size: 14px;
}

.skeleton-map__pulse {
  width: 24px;
  height: 24px;
}

.skeleton-locations__list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.skeleton-location {
  padding: 10px 12px;
  border-radius: 6px;
  border: 1px solid var(--n-border-color);
  background: var(--n-color-modal);
  transition: all 0.3s ease;
}

.skeleton-location--done {
  border-color: #18a05840;
  background: #18a05808;
}

/* 故事线骨架 */
.skeleton-storyline {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.skeleton-storyline__card {
  padding: 16px;
  border-radius: 8px;
  border: 1px solid var(--n-border-color);
  background: var(--n-color-modal);
  animation: shimmer-card 1.5s ease-in-out infinite;
  background-size: 200% 100%;
  background-image: linear-gradient(90deg, var(--n-color-modal) 25%, rgba(32, 128, 240, 0.04) 50%, var(--n-color-modal) 75%);
}

@keyframes shimmer-card {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

/* 加载文字动画 */
.loading-dots::after {
  content: '';
  animation: dots 1.2s steps(3, end) infinite;
}

@keyframes dots {
  0% { content: ''; }
  33% { content: '.'; }
  66% { content: '..'; }
  100% { content: '...'; }
}

@media (max-width: 720px) {
  .worldbuilding-progress {
    grid-template-columns: 1fr;
  }

  .worldbuilding-progress__rail {
    padding: 10px;
  }

  .worldbuilding-progress__list {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .worldbuilding-progress__rail-foot {
    display: none;
  }

  .worldbuilding-progress__preview {
    min-height: 250px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .skeleton-dot__pulse,
  .worldbuilding-progress__legend-dot--active,
  .worldbuilding-progress__live-dot,
  .skeleton-bar--shimmer,
  .worldbuilding-progress__waiting-lines span,
  .skeleton-storyline__card,
  .loading-dots::after {
    animation: none;
  }

  .worldbuilding-progress__item,
  .skeleton-dimension,
  .skeleton-character,
  .skeleton-location {
    transition: none;
  }
}
</style>

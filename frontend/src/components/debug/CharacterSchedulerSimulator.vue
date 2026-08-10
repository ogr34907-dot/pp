<template>
  <div class="character-scheduler-simulator">
    <div class="simulator-header">
      <h2 class="simulator-title">
        <n-icon class="title-icon"><PulseOutline /></n-icon>
        角色上下文调度模拟器
      </h2>
      <p class="simulator-desc">
        基于 <code>AppearanceScheduler</code> 和 <code>CharacterRegistry</code> 的排序算法
      </p>
    </div>

    <div class="simulator-body">
      <!-- 控制面板 -->
      <div class="control-panel">
        <h3 class="panel-title">
          <n-icon class="title-icon"><OptionsOutline /></n-icon>
          调度参数
        </h3>

        <!-- 大纲提及开关 -->
        <div class="control-group">
          <div class="control-item">
            <label class="control-label">大纲中提及艾达</label>
            <button
              type="button"
              class="toggle-switch"
              :class="{ active: mentionedAda }"
              :aria-pressed="mentionedAda"
              aria-label="切换大纲中提及艾达"
              @click="mentionedAda = !mentionedAda"
            ><span class="toggle-slider"></span></button>
          </div>

          <div class="control-item">
            <label class="control-label">大纲中提及苏晴</label>
            <button
              type="button"
              class="toggle-switch"
              :class="{ active: mentionedSuQing }"
              :aria-pressed="mentionedSuQing"
              aria-label="切换大纲中提及苏晴"
              @click="mentionedSuQing = !mentionedSuQing"
            ><span class="toggle-slider"></span></button>
          </div>
        </div>

        <!-- 最大角色数滑块 -->
        <div class="control-group">
          <label class="control-label">
            最大召回角色数量: <strong>{{ maxCharacters }}</strong>
          </label>
          <input
            type="range"
            v-model.number="maxCharacters"
            min="1"
            max="3"
            class="slider"
          />
          <div class="slider-labels">
            <span>1</span>
            <span>2</span>
            <span>3</span>
          </div>
        </div>
      </div>

      <!-- 角色卡片 -->
      <div class="characters-panel">
        <h3 class="panel-title">
          <n-icon class="title-icon"><PeopleOutline /></n-icon>
          角色库
        </h3>

        <div class="characters-grid">
          <div
            v-for="char in allCharacters"
            :key="char.id"
            class="character-card"
            :class="{
              mentioned: isMentioned(char.name),
              selected: isSelected(char),
              excluded: !isSelected(char) && isInQueue(char)
            }"
          >
            <div class="char-header">
              <span class="char-name">{{ char.name }}</span>
              <span class="char-importance" :class="`importance-${char.importanceLevel}`">
                {{ char.importance }}
              </span>
            </div>

            <div class="char-stats">
              <div class="stat-item">
                <span class="stat-label">活动度:</span>
                <span class="stat-value">{{ char.activityCount }}</span>
              </div>
              <div class="stat-item">
                <span class="stat-label">心理状态:</span>
                <span class="stat-value">{{ char.mentalState }}</span>
              </div>
            </div>

            <div class="char-behaviors">
              <div class="behavior-item">
                <span class="behavior-label">待机动作:</span>
                <span class="behavior-value">{{ char.idleBehavior }}</span>
              </div>
            </div>

            <div class="char-badges">
              <span v-if="isMentioned(char.name)" class="badge badge-mentioned">
                ✓ 大纲提及
              </span>
              <span v-if="isSelected(char)" class="badge badge-selected">
                ✓ 进入上下文
              </span>
              <span v-else-if="isInQueue(char)" class="badge badge-excluded">
                ✗ 已截断
              </span>
            </div>
          </div>
        </div>
      </div>

      <!-- 调度队列 -->
      <div class="queue-panel">
        <h3 class="panel-title">
          <n-icon class="title-icon"><ListOutline /></n-icon>
          调度队列
        </h3>

        <div class="queue-list">
          <div
            v-for="(char, index) in sortedQueue"
            :key="char.id"
            class="queue-item"
            :class="{
              selected: index < maxCharacters,
              excluded: index >= maxCharacters
            }"
          >
            <div class="queue-rank">{{ index + 1 }}</div>
            <div class="queue-info">
              <span class="queue-name">{{ char.name }}</span>
              <span class="queue-reason">{{ char.reason }}</span>
            </div>
            <div class="queue-status">
              <span v-if="index < maxCharacters" class="status-selected">✓ 入选</span>
              <span v-else class="status-excluded">✗ 超出配额</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 生成的上下文 -->
      <div class="context-panel">
        <h3 class="panel-title">
          <n-icon class="title-icon"><DocumentTextOutline /></n-icon>
          生成的上下文 Prompt
        </h3>

        <div class="context-output">
          <pre>{{ generatedContext }}</pre>
        </div>

        <div class="context-stats">
          <span class="stat">选中角色: {{ selectedCharacters.length }}</span>
          <span class="stat">预计 Token: {{ estimatedTokens }}</span>
        </div>
      </div>

      <!-- 算法说明 -->
      <div class="algorithm-panel">
        <h3 class="panel-title">
          <n-icon class="title-icon"><GitBranchOutline /></n-icon>
          排序算法逻辑
        </h3>

        <div class="algorithm-steps">
          <div class="algorithm-step">
            <div class="step-number">1</div>
            <div class="step-content">
              <strong>第一优先级：大纲提及</strong>
              <p>大纲中提到的角色享有最高优先级，直接排在队列前面</p>
            </div>
          </div>

          <div class="algorithm-step">
            <div class="step-number">2</div>
            <div class="step-content">
              <strong>第二优先级：角色重要性</strong>
              <p>主角 &gt; 主要配角 &gt; 重要配角 &gt; 次要角色 &gt; 背景角色</p>
            </div>
          </div>

          <div class="algorithm-step">
            <div class="step-number">3</div>
            <div class="step-content">
              <strong>第三优先级：活动度</strong>
              <p>出场次数越多，优先级越高（保持角色活跃度）</p>
            </div>
          </div>

          <div class="algorithm-step">
            <div class="step-number">4</div>
            <div class="step-content">
              <strong>截断策略</strong>
              <p>根据 Token 配额限制，从队列头部截取前 N 个角色</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import {
  DocumentTextOutline,
  GitBranchOutline,
  ListOutline,
  OptionsOutline,
  PeopleOutline,
  PulseOutline,
} from '@vicons/ionicons5'

// 角色数据
interface Character {
  id: string
  name: string
  importance: string
  importanceLevel: 'protagonist' | 'major' | 'minor'
  activityCount: number
  mentalState: string
  idleBehavior: string
}

const allCharacters = ref<Character[]>([
  {
    id: 'char-001',
    name: '林羽',
    importance: '主角',
    importanceLevel: 'protagonist',
    activityCount: 50,
    mentalState: 'NORMAL',
    idleBehavior: '摸剑柄'
  },
  {
    id: 'char-002',
    name: '艾达',
    importance: '次要角色',
    importanceLevel: 'minor',
    activityCount: 1,
    mentalState: '冷漠',
    idleBehavior: '擦拭机械臂'
  },
  {
    id: 'char-003',
    name: '苏晴',
    importance: '主要配角',
    importanceLevel: 'major',
    activityCount: 30,
    mentalState: '担忧',
    idleBehavior: '咬嘴唇'
  }
])

// 控制参数
const mentionedAda = ref(true)
const mentionedSuQing = ref(false)
const maxCharacters = ref(2)

// 重要性优先级映射
const importancePriority = {
  protagonist: 0,
  major: 1,
  minor: 2
}

// 判断角色是否在大纲中提及
const isMentioned = (name: string): boolean => {
  if (name === '艾达') return mentionedAda.value
  if (name === '苏晴') return mentionedSuQing.value
  return false
}

// 排序算法（核心逻辑）
const sortedQueue = computed(() => {
  const mentioned: Array<Character & { reason: string }> = []
  const notMentioned: Array<Character & { reason: string }> = []

  // 分类：提及 vs 未提及
  allCharacters.value.forEach(char => {
    const charWithReason = {
      ...char,
      reason: isMentioned(char.name) ? '大纲提及' : ''
    }

    if (isMentioned(char.name)) {
      mentioned.push(charWithReason)
    } else {
      notMentioned.push(charWithReason)
    }
  })

  // 对未提及的角色排序：重要性 > 活动度
  notMentioned.sort((a, b) => {
    const priorityDiff = importancePriority[a.importanceLevel] - importancePriority[b.importanceLevel]
    if (priorityDiff !== 0) {
      a.reason = `重要性: ${a.importance}`
      b.reason = `重要性: ${b.importance}`
      return priorityDiff
    }

    const activityDiff = b.activityCount - a.activityCount
    if (activityDiff !== 0) {
      a.reason = `活动度: ${a.activityCount}`
      b.reason = `活动度: ${b.activityCount}`
      return activityDiff
    }

    return 0
  })

  // 合并：提及的角色 + 排序后的未提及角色
  return [...mentioned, ...notMentioned]
})

// 选中的角色
const selectedCharacters = computed(() => {
  return sortedQueue.value.slice(0, maxCharacters.value)
})

// 判断角色是否被选中
const isSelected = (char: Character): boolean => {
  return selectedCharacters.value.some(c => c.id === char.id)
}

// 判断角色是否在队列中
const isInQueue = (char: Character): boolean => {
  return sortedQueue.value.some(c => c.id === char.id)
}

// 生成上下文 Prompt
const generatedContext = computed(() => {
  let context = '【角色设定约束】\n\n'

  selectedCharacters.value.forEach(char => {
    context += `角色：${char.name}\n`
    context += `描述：${char.importance}\n`
    context += `心理状态：${char.mentalState}\n`
    context += `待机动作：${char.idleBehavior}\n`

    // 如果角色刚登场，添加连续性约束
    if (char.activityCount <= 1) {
      context += `[连续性约束] ${char.name} 刚在上一章出场，需保持人设一致性。\n`
    }

    context += '\n'
  })

  return context
})

// 预估 Token 数
const estimatedTokens = computed(() => {
  // 粗略估算：1 token ≈ 4 字符
  return Math.ceil(generatedContext.value.length / 4)
})
</script>

<style scoped>
.character-scheduler-simulator {
  min-height: 100vh;
  padding: clamp(16px, 3vw, 36px);
  color: var(--app-text-primary);
  background: var(--app-page-bg);
  font-family: var(--font-sans);
  margin: 0 auto;
}

.simulator-header {
  max-width: 1400px;
  margin: 0 auto 24px;
  padding-bottom: 18px;
  border-bottom: 1px solid var(--app-border);
}

.simulator-title {
  font-size: clamp(22px, 3vw, 28px);
  font-weight: 650;
  font-family: var(--font-serif);
  margin: 0 0 8px;
  display: flex;
  align-items: center;
  gap: 10px;
}

.title-icon {
  font-size: 1.1em;
  color: var(--color-brand);
}

.simulator-desc {
  color: var(--app-text-secondary);
  font-size: 14px;
  margin: 0;
}

.simulator-desc code {
  background: var(--app-surface-subtle);
  border: 1px solid var(--app-border);
  padding: 2px 8px;
  border-radius: var(--app-radius-sm);
  color: var(--color-brand);
  font-family: var(--font-mono);
}

.simulator-body {
  max-width: 1400px;
  margin: 0 auto;
  display: grid;
  grid-template-columns: 1fr 2fr;
  gap: 24px;
}

@media (max-width: 1200px) {
  .simulator-body {
    grid-template-columns: 1fr;
  }
}

.panel-title {
  font-size: 18px;
  font-weight: 600;
  margin: 0 0 16px 0;
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--app-text-primary);
}

.control-panel,
.characters-panel,
.queue-panel,
.context-panel,
.algorithm-panel {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-md);
  padding: 20px;
  box-shadow: var(--app-shadow-sm);
}

.control-group {
  margin-bottom: 24px;
}

.control-group:last-child {
  margin-bottom: 0;
}

.control-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.control-label {
  font-size: 14px;
  color: var(--app-text-secondary);
}

.toggle-switch {
  width: 56px;
  height: 28px;
  padding: 0;
  border: 1px solid var(--app-border-strong);
  background: var(--app-surface-subtle);
  border-radius: 14px;
  cursor: pointer;
  position: relative;
  transition: background var(--app-transition), border-color var(--app-transition);
}

.toggle-switch.active {
  background: var(--color-brand);
  border-color: var(--color-brand);
}

.toggle-switch:focus-visible,
.slider:focus-visible {
  outline: 2px solid var(--color-focus);
  outline-offset: 3px;
}

.toggle-slider {
  width: 22px;
  height: 22px;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 50%;
  position: absolute;
  top: 3px;
  left: 3px;
  transition: left var(--app-transition);
}

.toggle-switch.active .toggle-slider {
  left: 31px;
}

.slider {
  width: 100%;
  height: 8px;
  border-radius: 4px;
  background: var(--app-surface-subtle);
  outline: none;
  -webkit-appearance: none;
  margin: 12px 0;
}

.slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: var(--color-brand);
  cursor: pointer;
  box-shadow: 0 0 0 3px var(--color-focus-soft);
}

.slider-labels {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: var(--app-text-secondary);
}

.characters-panel {
  grid-column: 1 / -1;
}

.characters-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 16px;
}

.character-card {
  background: var(--app-surface-subtle);
  border-radius: var(--app-radius-sm);
  padding: 16px;
  border: 1px solid var(--app-border);
  transition: border-color var(--app-transition), box-shadow var(--app-transition), opacity var(--app-transition);
}

.character-card.mentioned {
  border-color: var(--color-accent);
}

.character-card.selected {
  border-color: var(--color-brand);
  box-shadow: 0 0 0 2px var(--color-focus-soft);
}

.character-card.excluded {
  opacity: 0.5;
}

.char-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.char-name {
  font-size: 18px;
  font-weight: 600;
  color: var(--app-text-primary);
}

.char-importance {
  padding: 4px 10px;
  border-radius: 12px;
  font-size: 12px;
  font-weight: 600;
}

.importance-protagonist {
  background: var(--color-brand);
  color: var(--app-text-inverse);
}

.importance-major {
  background: var(--color-accent-soft);
  color: var(--app-text-primary);
  border: 1px solid var(--color-accent-border);
}

.importance-minor {
  background: var(--app-surface-subtle);
  color: var(--app-text-secondary);
  border: 1px solid var(--app-border);
}

.char-stats {
  margin-bottom: 12px;
}

.stat-item {
  display: flex;
  justify-content: space-between;
  margin-bottom: 6px;
  font-size: 13px;
}

.stat-label {
  color: var(--app-text-secondary);
}

.stat-value {
  color: var(--app-text-primary);
  font-weight: 600;
}

.char-behaviors {
  padding-top: 8px;
  border-top: 1px solid var(--app-divider);
}

.behavior-item {
  font-size: 13px;
}

.behavior-label {
  color: var(--app-text-secondary);
}

.behavior-value {
  color: var(--color-brand);
  margin-left: 6px;
}

.char-badges {
  margin-top: 12px;
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.badge {
  padding: 4px 8px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 600;
}

.badge-mentioned {
  background: var(--color-accent-soft);
  color: var(--app-text-primary);
  border: 1px solid var(--color-accent-border);
}

.badge-selected {
  background: var(--color-brand-light);
  color: var(--color-brand);
  border: 1px solid var(--color-brand-border);
}

.badge-excluded {
  background: var(--app-surface-subtle);
  color: var(--app-text-secondary);
  border: 1px solid var(--app-border);
}

.queue-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.queue-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px;
  background: var(--app-surface-subtle);
  border-radius: var(--app-radius-sm);
  border: 1px solid var(--app-border);
  border-left: 4px solid var(--app-border-strong);
  transition: border-color var(--app-transition), background var(--app-transition), opacity var(--app-transition);
}

.queue-item.selected {
  border-left-color: var(--color-brand);
  background: var(--color-brand-light);
}

.queue-item.excluded {
  opacity: 0.5;
}

.queue-rank {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: var(--app-border-strong);
  color: var(--app-text-primary);
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 14px;
}

.queue-item.selected .queue-rank {
  background: var(--color-brand);
  color: var(--app-text-inverse);
}

.queue-info {
  flex: 1;
}

.queue-name {
  display: block;
  font-size: 15px;
  font-weight: 600;
  color: var(--app-text-primary);
  margin-bottom: 4px;
}

.queue-reason {
  font-size: 12px;
  color: var(--app-text-secondary);
}

.queue-status {
  font-size: 13px;
  font-weight: 600;
}

.status-selected {
  color: var(--color-brand);
}

.status-excluded {
  color: var(--app-text-secondary);
}

.context-panel {
  grid-column: 1 / -1;
}

.context-output {
  background: var(--app-surface-subtle);
  border-radius: var(--app-radius-sm);
  padding: 16px;
  border: 1px solid var(--app-border);
  margin-bottom: 16px;
}

.context-output pre {
  margin: 0;
  color: var(--app-text-primary);
  font-family: var(--font-mono);
  font-size: 14px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-wrap: break-word;
}

.context-stats {
  display: flex;
  gap: 16px;
}

.context-stats .stat {
  font-size: 13px;
  color: var(--app-text-secondary);
}

.algorithm-panel {
  grid-column: 1 / -1;
}

.algorithm-steps {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 16px;
}

.algorithm-step {
  display: flex;
  gap: 12px;
  padding: 16px;
  background: var(--app-surface-subtle);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-sm);
}

.step-number {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  background: var(--color-brand);
  color: var(--app-text-inverse);
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 16px;
  flex-shrink: 0;
}

.step-content strong {
  display: block;
  margin-bottom: 6px;
  color: var(--app-text-primary);
  font-size: 14px;
}

.step-content p {
  margin: 0;
  font-size: 13px;
  color: var(--app-text-secondary);
  line-height: 1.5;
}

@media (max-width: 640px) {
  .character-scheduler-simulator {
    padding: 14px 10px 24px;
  }

  .simulator-body,
  .characters-grid,
  .algorithm-steps {
    grid-template-columns: 1fr;
    gap: 12px;
  }

  .control-panel,
  .characters-panel,
  .queue-panel,
  .context-panel,
  .algorithm-panel {
    padding: 16px;
  }

  .queue-item {
    align-items: flex-start;
    flex-wrap: wrap;
  }

  .queue-status {
    width: 100%;
    padding-left: 44px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .toggle-switch,
  .toggle-slider,
  .character-card,
  .queue-item {
    transition: none;
  }
}
</style>

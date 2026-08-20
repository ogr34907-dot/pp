<template>
  <section class="continuity-review" aria-label="连续性审查">
    <div class="continuity-review__head">
      <div>
        <span class="continuity-review__kicker">连续性审查</span>
        <h3>{{ stateLabel }}</h3>
      </div>
      <n-tag v-if="decision" size="small" :type="decisionType">{{ decisionLabel }}</n-tag>
    </div>

    <n-alert v-if="technicalBlockers.length" type="error" :show-icon="true">
      <ul class="continuity-review__list">
        <li v-for="item in technicalBlockers" :key="item">{{ item }}</li>
      </ul>
    </n-alert>
    <n-alert v-else-if="issues.length" type="warning" :show-icon="true">
      <ul class="continuity-review__list">
        <li v-for="item in issues" :key="item.id || item.message" class="continuity-review__issue">
          <span>{{ item.message || item.code }}</span>
          <small v-if="item.evidence_refs?.length">{{ item.evidence_refs.join(' · ') }}</small>
          <span v-if="item.suggestion" class="continuity-review__suggestion">{{ item.suggestion }}</span>
          <n-button
            v-if="canApplySuggestion(item)"
            size="small"
            tertiary
            :loading="loading"
            @click="$emit('applySuggestion', { suggestionId: item.id })"
          >
            应用建议
          </n-button>
        </li>
      </ul>
    </n-alert>
    <p v-else class="continuity-review__empty">尚未生成当前范围的审查报告。</p>

    <n-input
      v-if="needsReason"
      v-model:value="reason"
      type="textarea"
      :rows="3"
      placeholder="确认前填写原因"
      aria-label="确认原因"
    />
    <div class="continuity-review__actions">
      <n-button secondary :loading="loading" @click="$emit('review')">重新审查</n-button>
      <n-button
        v-if="canConfirm"
        type="primary"
        :disabled="technicalBlockers.length > 0 || (needsReason && !reason.trim())"
        :loading="loading"
        @click="$emit('acknowledge', { reason: reason.trim() })"
      >
        {{ decision === 'pass' ? '采用通过结果' : '确认风险并发布' }}
      </n-button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { OutlineContinuityStatus } from '@/api/generation'

const props = defineProps<{
  status: OutlineContinuityStatus | null
  loading?: boolean
  hardBlockers?: string[]
  lockedFields?: string[]
}>()

defineEmits<{
  review: []
  acknowledge: [payload: { reason: string }]
  applySuggestion: [payload: { suggestionId: string }]
}>()

const reason = defineModel<string>('reason', { default: '' })
const report = computed(() => (props.status?.current?.report || props.status?.latest?.report || {}) as Record<string, any>)
const issues = computed(() => Array.isArray(report.value.issues) ? report.value.issues : [])
const decision = computed(() => String(props.status?.current?.decision || props.status?.latest?.decision || ''))
const technicalBlockers = computed(() => [
  ...(props.hardBlockers || []),
  ...(props.status?.technical_blockers || []),
])
const needsReason = computed(() => decision.value !== '' && decision.value !== 'pass')
const canConfirm = computed(() => Boolean(props.status?.current && !technicalBlockers.value.length))
const canApplySuggestion = (issue: Record<string, any>) => {
  const patch = issue.suggested_patch
  return Boolean(
    props.status?.current
      && patch
      && typeof patch === 'object'
      && Object.keys(patch).every(field => !(props.lockedFields || []).includes(field))
      && !technicalBlockers.value.length,
  )
}
const stateLabel = computed(() => ({
  not_required: '未要求', pending: '待确认', pass: '可发布', acknowledged: '已确认',
} as Record<string, string>)[String(props.status?.state || 'pending')] || '待审查')
const decisionLabel = computed(() => ({
  pass: '通过', review: '需复核', conflict: '冲突', unavailable: '服务不可用',
} as Record<string, string>)[decision.value] || '未完成')
const decisionType = computed(() => ({
  pass: 'success', review: 'warning', conflict: 'error', unavailable: 'default',
} as Record<string, any>)[decision.value] || 'default')
</script>

<style scoped>
.continuity-review { display: grid; gap: 10px; }
.continuity-review__head { display: flex; align-items: start; justify-content: space-between; gap: 8px; }
.continuity-review__kicker { color: var(--app-text-muted); font-size: 11px; }
.continuity-review h3 { margin: 3px 0 0; font-size: 13px; }
.continuity-review__list { margin: 0; padding-left: 18px; }
.continuity-review__issue { display: grid; gap: 4px; margin: 0 0 8px; }
.continuity-review__issue small { color: var(--app-text-muted); font-family: var(--app-font-mono, ui-monospace, monospace); font-size: 11px; }
.continuity-review__suggestion { color: var(--app-text-secondary); font-size: 12px; }
.continuity-review__empty { margin: 0; color: var(--app-text-muted); font-size: 12px; }
.continuity-review__actions { display: flex; flex-wrap: wrap; gap: 8px; }
</style>

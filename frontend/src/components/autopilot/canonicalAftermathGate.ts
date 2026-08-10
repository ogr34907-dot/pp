import { ref } from 'vue'
import type { CanonicalAftermathFullResyncEvent } from '../../api/autopilot'

export interface CanonicalAftermathPresentation {
  isFailure: boolean
  title: string
  currentChapterAction: CanonicalAftermathRecoveryAction
  fullResyncAction: CanonicalAftermathRecoveryAction & {
    label: string
    scope: string
  }
  resumeAction: CanonicalAftermathRecoveryAction
  afterFullResyncCompleted: () => void | Promise<void>
  asyncStatus: {
    status: 'waiting' | 'running' | 'failed'
    stage: string
    message: string
    current?: number
    total?: number
  } | null
}

export interface CanonicalAftermathRecoveryAction {
  intent: 'retry' | 'resume' | 'resync-all'
  label: string
  disabled: boolean
  invoke: () => void | Promise<void>
}

export interface CanonicalAftermathRecoveryHandlers {
  retryCurrentChapter: () => void | Promise<void>
  resyncFullBook: () => void | Promise<void>
  resume: () => void | Promise<void>
  refresh: () => void | Promise<void>
}

export interface CanonicalAftermathFullResyncSnapshot {
  active: boolean
  processed: number
  total: number
  currentChapter: number | null
  firstFailureReason: string
}

export function canStartCanonicalAftermathFullResync(
  status: Record<string, any> | null | undefined,
  fullResyncActive: boolean,
): boolean {
  return !fullResyncActive
    && getCanonicalAftermathPresentation(status).isFailure
    && String(status?.current_stage || '') === 'paused_for_review'
}

export function createCanonicalAftermathFullResyncState() {
  const active = ref(false)
  const processed = ref(0)
  const total = ref(0)
  const synced = ref(0)
  const skipped = ref(0)
  const vectorFailed = ref(0)
  const currentChapter = ref<number | null>(null)
  const firstFailureReason = ref('')
  const completedListeners: Array<(event: CanonicalAftermathFullResyncEvent) => void> = []
  const updateProgress = (event: CanonicalAftermathFullResyncEvent) => {
    if (typeof event.processed === 'number') processed.value = event.processed
    if (typeof event.total === 'number') total.value = event.total
    if (typeof event.synced === 'number') synced.value = event.synced
    if (typeof event.skipped === 'number') skipped.value = event.skipped
    if (Array.isArray(event.vector_failed)) vectorFailed.value = event.vector_failed.length
    if (typeof event.chapter_number === 'number') currentChapter.value = event.chapter_number
  }
  const recordFailure = (event: CanonicalAftermathFullResyncEvent) => {
    if (!firstFailureReason.value) {
      updateProgress(event)
      firstFailureReason.value = String(
        event.failure_reason || event.reason || event.message || event.status || '同步失败',
      )
    } else if (typeof event.total === 'number') {
      total.value = event.total
    }
  }
  const recordVector = (event: CanonicalAftermathFullResyncEvent) => {
    updateProgress(event)
    if (event.status && event.status !== 'stored') vectorFailed.value += 1
  }
  return {
    active,
    processed,
    total,
    synced,
    skipped,
    vectorFailed,
    currentChapter,
    firstFailureReason,
    shouldAutoResume: false,
    started(event: CanonicalAftermathFullResyncEvent) {
      active.value = true
      updateProgress(event)
    },
    chapter: updateProgress,
    vector: recordVector,
    failed: recordFailure,
    completed(event: CanonicalAftermathFullResyncEvent) {
      updateProgress(event)
      active.value = false
      completedListeners.splice(0).forEach(listener => listener(event))
    },
    cancelled(event: CanonicalAftermathFullResyncEvent) {
      updateProgress(event)
      active.value = false
    },
    onCompleted(listener: (event: CanonicalAftermathFullResyncEvent) => void) {
      completedListeners.push(listener)
    },
    reset() {
      active.value = false
      processed.value = 0
      total.value = 0
      synced.value = 0
      skipped.value = 0
      vectorFailed.value = 0
      currentChapter.value = null
      firstFailureReason.value = ''
    },
  }
}

export function shouldShowCanonicalAftermathFullResyncProgress(
  active: boolean,
  firstFailureReason: string,
): boolean {
  return active || Boolean(firstFailureReason)
}

export function canOfferReviewResume(
  baselineCanResume: boolean,
  fullResyncActive: boolean,
): boolean {
  return baselineCanResume && !fullResyncActive
}

export function getCanonicalAftermathPresentation(
  status: Record<string, any> | null | undefined,
  fullResync?: CanonicalAftermathFullResyncSnapshot,
  handlers?: CanonicalAftermathRecoveryHandlers,
): CanonicalAftermathPresentation {
  const gate = status?.review_gate
  const isFailure = String(status?.autopilot_pause_reason || '') === 'canonical_aftermath_not_ready'
    || String(gate?.type || '') === 'canonical_aftermath'
  const fullResyncActive = fullResync?.active === true
  const guardedInvoke = (
    disabled: boolean,
    handler: (() => void | Promise<void>) | undefined,
  ) => () => {
    if (!disabled) return handler?.()
  }
  const currentChapterDisabled = !isFailure || fullResyncActive
  const fullResyncDisabled = !isFailure
    || fullResyncActive
    || String(status?.current_stage || '') !== 'paused_for_review'
  const currentChapterLabel = String(gate?.action_label || '重新同步本章')
  const currentChapterAction: CanonicalAftermathRecoveryAction = {
    intent: 'retry',
    label: currentChapterLabel,
    disabled: currentChapterDisabled,
    invoke: guardedInvoke(currentChapterDisabled, handlers?.retryCurrentChapter),
  }
  const fullResyncAction: CanonicalAftermathPresentation['fullResyncAction'] = {
    intent: 'resync-all',
    label: '从第 1 章开始全流程同步',
    scope: '第 1 章至当前章',
    disabled: fullResyncDisabled,
    invoke: guardedInvoke(fullResyncDisabled, handlers?.resyncFullBook),
  }
  const resumeDisabled = isFailure || fullResyncActive
  const resumeAction: CanonicalAftermathRecoveryAction = {
    intent: 'resume',
    label: '继续自动驾驶',
    disabled: resumeDisabled,
    invoke: guardedInvoke(resumeDisabled, handlers?.resume),
  }
  const afterFullResyncCompleted = () => handlers?.refresh()

  if (!isFailure) {
    return {
      isFailure: false,
      title: '',
      currentChapterAction,
      fullResyncAction,
      resumeAction,
      afterFullResyncCompleted,
      asyncStatus: null,
    }
  }

  const firstFailureReason = String(fullResync?.firstFailureReason || '')
  const currentChapter = Number(fullResync?.currentChapter || 0)
  const asyncStatus = fullResync
    ? {
        status: fullResyncActive ? 'running' as const : (firstFailureReason ? 'failed' as const : 'waiting' as const),
        stage: '第 1 章至当前章',
        message: firstFailureReason
          ? `同步中断：${firstFailureReason}`
          : (currentChapter > 0 ? `正在同步第 ${currentChapter} 章` : '准备从第 1 章开始同步'),
        current: Math.max(0, Number(fullResync.processed || 0)),
        total: Math.max(0, Number(fullResync.total || 0)),
      }
    : null

  return {
    isFailure: true,
    title: '规范章后同步失败',
    currentChapterAction,
    fullResyncAction,
    resumeAction,
    afterFullResyncCompleted,
    asyncStatus,
  }
}

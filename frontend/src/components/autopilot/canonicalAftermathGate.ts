import { ref } from 'vue'
import type { CanonicalAftermathFullResyncEvent } from '../../api/autopilot'

export interface CanonicalAftermathPresentation {
  isFailure: boolean
  title: string
  actionLabel: string
  canResume: boolean
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
      firstFailureReason.value = String(event.reason || event.message || event.status || '同步失败')
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

export function getCanonicalAftermathPresentation(
  status: Record<string, any> | null | undefined,
): CanonicalAftermathPresentation {
  const gate = status?.review_gate
  const isFailure = String(status?.autopilot_pause_reason || '') === 'canonical_aftermath_not_ready'
    || String(gate?.type || '') === 'canonical_aftermath'

  if (!isFailure) {
    return {
      isFailure: false,
      title: '',
      actionLabel: '',
      canResume: true,
    }
  }

  return {
    isFailure: true,
    title: '规范章后同步失败',
    actionLabel: String(gate?.action_label || '重新同步本章'),
    canResume: false,
  }
}

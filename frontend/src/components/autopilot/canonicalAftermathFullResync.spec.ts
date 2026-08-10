import { describe, expect, it } from 'vitest'
import {
  canStartCanonicalAftermathFullResync,
  canOfferReviewResume,
  createCanonicalAftermathFullResyncState,
  getCanonicalAftermathPresentation,
  shouldShowCanonicalAftermathFullResyncProgress,
} from './canonicalAftermathGate'

describe('canonical aftermath full resync presentation state', () => {
  it('tracks processed/total, current chapter, and first failure reason', () => {
    const state = createCanonicalAftermathFullResyncState()
    state.started({ type: 'started', total: 4, processed: 0 })
    state.chapter({ type: 'chapter', chapter_number: 8, processed: 1, total: 4 })
    state.vector({ type: 'vector', chapter_number: 8, status: 'failed', processed: 1, total: 4 })
    state.failed({
      type: 'failed',
      chapter_number: 8,
      failure_reason: '持久化向量失败',
      reason: '向量写入失败',
      processed: 1,
      total: 4,
    })
    state.failed({ type: 'failed', chapter_number: 9, reason: 'later failure', processed: 2, total: 4 })

    expect(state.processed.value).toBe(1)
    expect(state.total.value).toBe(4)
    expect(state.currentChapter.value).toBe(8)
    expect(state.firstFailureReason.value).toBe('持久化向量失败')
    expect(state.vectorFailed.value).toBe(1)
  })

  it('keeps progress visible from local active state or terminal diagnostics', () => {
    expect(shouldShowCanonicalAftermathFullResyncProgress(true, '')).toBe(true)
    expect(shouldShowCanonicalAftermathFullResyncProgress(false, '持久化向量失败')).toBe(true)
    expect(shouldShowCanonicalAftermathFullResyncProgress(false, '')).toBe(false)
  })

  it('withholds review resume controls while the local full-resync stream is active', () => {
    const state = createCanonicalAftermathFullResyncState()
    state.started({ type: 'started', total: 4, processed: 0 })

    expect(canOfferReviewResume(true, state.active.value)).toBe(false)

    state.completed({ type: 'completed', total: 4, processed: 4 })
    expect(canOfferReviewResume(true, state.active.value)).toBe(true)
  })

  it('enables full-book resync for a canonical failure paused for review even while autopilot remains running', () => {
    expect(canStartCanonicalAftermathFullResync({
      autopilot_pause_reason: 'canonical_aftermath_not_ready',
      autopilot_status: 'running',
      current_stage: 'paused_for_review',
    }, false)).toBe(true)
  })

  it('exposes canonical failure as an assertive alert with an explicit full-book recovery intent', () => {
    const presentation = getCanonicalAftermathPresentation({
      autopilot_pause_reason: 'canonical_aftermath_not_ready',
      autopilot_status: 'running',
      current_stage: 'paused_for_review',
      current_chapter_number: 64,
      review_gate: {
        type: 'canonical_aftermath',
        message: '第 64 章的规范记忆同步尚未完成',
      },
    }) as any

    expect(presentation).toMatchObject({
      role: 'alert',
      ariaLive: 'assertive',
      fullResyncAction: {
        intent: 'resync-all',
        label: '从第 1 章开始全流程同步',
        scope: '第 1 章至当前章',
        disabled: false,
      },
    })
  })

  it('presents active full-book counters while disabling recovery and never auto-resuming', () => {
    const presentation = getCanonicalAftermathPresentation({
      autopilot_pause_reason: 'canonical_aftermath_not_ready',
      current_stage: 'paused_for_review',
      current_chapter_number: 64,
    }, {
      active: true,
      processed: 18,
      total: 64,
      currentChapter: 19,
      firstFailureReason: '',
    }) as any

    expect(presentation.fullResyncAction.disabled).toBe(true)
    expect(presentation.asyncStatus).toMatchObject({
      status: 'running',
      stage: '第 1 章至当前章',
      current: 18,
      total: 64,
      message: '正在同步第 19 章',
      shouldAutoResume: false,
    })
    expect(presentation.canResume).toBe(false)
  })

  it('marks completion without requesting a resume', () => {
    const state = createCanonicalAftermathFullResyncState()
    let completed = 0
    state.onCompleted(() => { completed += 1 })
    state.completed({ type: 'completed', processed: 2, total: 2 })
    expect(state.active.value).toBe(false)
    expect(completed).toBe(1)
    expect(state.shouldAutoResume).toBe(false)
  })
})

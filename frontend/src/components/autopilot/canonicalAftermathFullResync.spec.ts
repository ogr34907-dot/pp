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

  it('routes the consumed recovery actions to current-chapter/full-book handlers and never resumes on completion', () => {
    let currentChapterCalls = 0
    let fullBookCalls = 0
    let resumeCalls = 0
    let refreshCalls = 0
    const presentation = getCanonicalAftermathPresentation({
      autopilot_pause_reason: 'canonical_aftermath_not_ready',
      autopilot_status: 'running',
      current_stage: 'paused_for_review',
      current_chapter_number: 64,
      review_gate: {
        type: 'canonical_aftermath',
        message: '第 64 章的规范记忆同步尚未完成',
      },
    }, undefined, {
      retryCurrentChapter: () => { currentChapterCalls += 1 },
      resyncFullBook: () => { fullBookCalls += 1 },
      resume: () => { resumeCalls += 1 },
      refresh: () => { refreshCalls += 1 },
    })

    expect(presentation.currentChapterAction).toMatchObject({
      intent: 'retry',
      disabled: false,
    })
    expect(presentation.fullResyncAction).toMatchObject({
      intent: 'resync-all',
      scope: '第 1 章至当前章',
      disabled: false,
    })
    expect(presentation.resumeAction.disabled).toBe(true)

    presentation.currentChapterAction.invoke()
    presentation.fullResyncAction.invoke()
    presentation.resumeAction.invoke()
    presentation.afterFullResyncCompleted()

    expect(currentChapterCalls).toBe(1)
    expect(fullBookCalls).toBe(1)
    expect(refreshCalls).toBe(1)
    expect(resumeCalls).toBe(0)
  })

  it('invokes resume only for an eligible visible entry and blocks every entry while full sync is active', () => {
    let resumeCalls = 0
    let refreshCalls = 0
    const handlers = {
      retryCurrentChapter: () => {},
      resyncFullBook: () => {},
      resume: () => { resumeCalls += 1 },
      refresh: () => { refreshCalls += 1 },
    }
    const inactiveFullSync = {
      active: false,
      processed: 0,
      total: 0,
      currentChapter: null,
      firstFailureReason: '',
    }
    const eligibleReview = {
      canResumeReview: true,
      isManualPause: false,
    }
    const ineligible = {
      ...eligibleReview,
      canResumeReview: false,
    }
    const eligibleManualPause = {
      ...ineligible,
      isManualPause: true,
    }
    const activeFullSync = {
      ...inactiveFullSync,
      active: true,
    }
    const status = {
      autopilot_status: 'running',
      current_stage: 'paused_for_review',
      review_gate: { action_label: '确认后继续' },
    }

    const eligiblePresentation = getCanonicalAftermathPresentation(status, inactiveFullSync, handlers, eligibleReview)
    eligiblePresentation.resumeAction.invoke()
    expect(resumeCalls).toBe(1)

    const manualPresentation = getCanonicalAftermathPresentation(status, inactiveFullSync, handlers, eligibleManualPause)
    manualPresentation.resumeAction.invoke()
    expect(resumeCalls).toBe(2)

    const ineligiblePresentation = getCanonicalAftermathPresentation(status, inactiveFullSync, handlers, ineligible)
    ineligiblePresentation.resumeAction.invoke()
    expect(resumeCalls).toBe(2)

    const activePresentation = getCanonicalAftermathPresentation(status, activeFullSync, handlers, eligibleReview)
    activePresentation.resumeAction.invoke()
    activePresentation.resumeAction.invoke()
    activePresentation.afterFullResyncCompleted()
    expect(resumeCalls).toBe(2)
    expect(refreshCalls).toBe(1)

    expect(eligiblePresentation.resumeAction).toMatchObject({
      mode: 'review',
      visible: true,
      disabled: false,
    })
    expect(ineligiblePresentation.resumeAction).toMatchObject({
      mode: null,
      visible: false,
      disabled: true,
    })
    expect(manualPresentation.resumeAction).toMatchObject({
      mode: 'manual-pause',
      visible: true,
      disabled: false,
    })
    expect(activePresentation.resumeAction).toMatchObject({
      mode: 'review',
      visible: false,
      disabled: true,
    })
  })

  it('blocks every canonical recovery handler while full-book synchronization is active', () => {
    let currentChapterCalls = 0
    let fullBookCalls = 0
    let resumeCalls = 0
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
    }, {
      retryCurrentChapter: () => { currentChapterCalls += 1 },
      resyncFullBook: () => { fullBookCalls += 1 },
      resume: () => { resumeCalls += 1 },
      refresh: () => {},
    })

    expect(presentation.currentChapterAction.disabled).toBe(true)
    expect(presentation.fullResyncAction.disabled).toBe(true)
    expect(presentation.resumeAction.disabled).toBe(true)
    expect(presentation.asyncStatus).toMatchObject({
      status: 'running',
      stage: '第 1 章至当前章',
      current: 18,
      total: 64,
      message: '正在同步第 19 章',
    })

    presentation.currentChapterAction.invoke()
    presentation.fullResyncAction.invoke()
    presentation.resumeAction.invoke()

    expect(currentChapterCalls).toBe(0)
    expect(fullBookCalls).toBe(0)
    expect(resumeCalls).toBe(0)
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

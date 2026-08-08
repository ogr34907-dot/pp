import { describe, expect, it } from 'vitest'
import {
  canOfferReviewResume,
  createCanonicalAftermathFullResyncState,
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

import { describe, expect, it } from 'vitest'
import { createCanonicalAftermathFullResyncState } from './canonicalAftermathGate'

describe('canonical aftermath full resync presentation state', () => {
  it('tracks processed/total, current chapter, and first failure reason', () => {
    const state = createCanonicalAftermathFullResyncState()
    state.started({ type: 'started', total: 4, processed: 0 })
    state.chapter({ type: 'chapter', chapter_number: 8, processed: 1, total: 4 })
    state.vector({ type: 'vector', chapter_number: 8, status: 'failed', processed: 1, total: 4 })
    state.failed({ type: 'failed', chapter_number: 8, reason: '向量写入失败', processed: 1, total: 4 })
    state.failed({ type: 'failed', chapter_number: 9, reason: 'later failure', processed: 2, total: 4 })

    expect(state.processed.value).toBe(1)
    expect(state.total.value).toBe(4)
    expect(state.currentChapter.value).toBe(8)
    expect(state.firstFailureReason.value).toBe('向量写入失败')
    expect(state.vectorFailed.value).toBe(1)
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

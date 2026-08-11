import { describe, expect, it } from 'vitest'
import { getGenerationPresentation } from './generationPresentation'

describe('getGenerationPresentation', () => {
  it('uses the server candidate state instead of guessing writing from a legacy stage', () => {
    expect(getGenerationPresentation({
      state: 'waiting_review',
      run_mode: 'chapter_review',
      current_candidate_chapter: 41,
      canonical_sync_status: 'ready',
      next_action: 'author_review_candidate',
    })).toMatchObject({ key: 'waiting_review', label: '第 41 章待审核', tone: 'warning' })
  })

  it('keeps a failed formal sync above the next-generation action', () => {
    expect(getGenerationPresentation({
      state: 'paused',
      current_candidate_chapter: 7,
      canonical_sync_status: 'failed',
      next_action: 'retry_sync',
      last_error: 'canonical_aftermath_not_ready',
    })).toMatchObject({ key: 'sync_failed', label: '第 7 章已入库、同步失败', tone: 'error' })
  })

  it('shows an explicitly stopped run as stopped even when a stale legacy card says writing', () => {
    expect(getGenerationPresentation({ state: 'stopped', next_action: 'idle' })).toMatchObject({
      key: 'stopped', label: '已终止', tone: 'neutral',
    })
  })
})

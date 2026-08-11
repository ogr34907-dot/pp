import { describe, expect, it } from 'vitest'
import { getCandidateGenerationProgress } from './candidateGenerationProgress'

describe('getCandidateGenerationProgress', () => {
  it('marks candidate generation as the active first stage', () => {
    const progress = getCandidateGenerationProgress({
      state: 'running',
      run_mode: 'continuous',
      current_formal_chapter: 4,
      target_chapters: 8,
      current_candidate_chapter: 5,
      canonical_sync_status: 'ready',
      candidate: { status: 'streaming', chapter_number: 5 },
    })

    expect(progress).toMatchObject({
      candidateChapter: 5,
      formalProgress: 50,
      activeStep: 0,
      tone: 'brand',
    })
    expect(progress.steps.map(step => step.state)).toEqual([
      'active', 'pending', 'pending', 'pending', 'pending',
    ])
  })

  it('keeps the next chapter behind an author-review gate', () => {
    const progress = getCandidateGenerationProgress({
      state: 'waiting_review',
      run_mode: 'chapter_review',
      current_formal_chapter: 4,
      target_chapters: 8,
      current_candidate_chapter: 5,
      canonical_sync_status: 'ready',
      candidate: { status: 'awaiting_review', chapter_number: 5 },
    })

    expect(progress).toMatchObject({ activeStep: 2, tone: 'warning' })
    expect(progress.steps.map(step => step.state)).toEqual([
      'complete', 'complete', 'attention', 'pending', 'pending',
    ])
  })

  it('shows a failed canonical sync after formal writing has completed', () => {
    const progress = getCandidateGenerationProgress({
      state: 'paused',
      run_mode: 'continuous',
      current_formal_chapter: 5,
      target_chapters: 8,
      current_candidate_chapter: 5,
      canonical_sync_status: 'failed',
      candidate: { status: 'syncing', chapter_number: 5 },
    })

    expect(progress).toMatchObject({ activeStep: 4, tone: 'error' })
    expect(progress.steps.map(step => step.state)).toEqual([
      'complete', 'complete', 'complete', 'complete', 'failed',
    ])
  })
})

import { describe, expect, it } from 'vitest'
import { isReviewDraftDirty, type ReviewDraft } from './reviewDraft'

const saved: ReviewDraft = {
  content: '候选正文',
  feedback: '保留伏笔',
  chapterSummary: '章节摘要',
  eventsText: '事件一',
  handoffText: '交接一',
}

describe('isReviewDraftDirty', () => {
  it('keeps a matching server snapshot clean', () => {
    expect(isReviewDraftDirty(saved, saved)).toBe(false)
  })

  it('detects unsaved edits in every author-editable review field', () => {
    expect(isReviewDraftDirty({ ...saved, content: '作者改稿' }, saved)).toBe(true)
    expect(isReviewDraftDirty({ ...saved, feedback: '调整节奏' }, saved)).toBe(true)
    expect(isReviewDraftDirty({ ...saved, chapterSummary: '新摘要' }, saved)).toBe(true)
    expect(isReviewDraftDirty({ ...saved, eventsText: '事件二' }, saved)).toBe(true)
    expect(isReviewDraftDirty({ ...saved, handoffText: '交接二' }, saved)).toBe(true)
  })
})

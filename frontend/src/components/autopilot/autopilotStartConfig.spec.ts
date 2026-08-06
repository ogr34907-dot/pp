import { describe, expect, it } from 'vitest'
import { normalizeAutopilotStartConfig } from './autopilotStartConfig'

describe('normalizeAutopilotStartConfig', () => {
  it('keeps a one-chapter safety cap for a five-hundred-chapter book', () => {
    expect(normalizeAutopilotStartConfig({
      target_chapters: 500,
      target_words_per_chapter: 2000,
      max_auto_chapters: 1,
      auto_approve_mode: false,
    })).toEqual({
      target_chapters: 500,
      target_words_per_chapter: 2000,
      max_auto_chapters: 1,
      auto_approve_mode: false,
    })
  })
})

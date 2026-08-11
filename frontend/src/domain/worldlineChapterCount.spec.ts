import { describe, expect, it } from 'vitest'
import { normalizeWorldlineTarget, resolveGeneratedChapterCount } from './worldlineChapterCount'

describe('resolveGeneratedChapterCount', () => {
  it('uses existing formal chapters before a generation run has been created', () => {
    expect(resolveGeneratedChapterCount(null, [{ number: 1 }, { number: 4 }])).toBe(4)
  })

  it('keeps the larger authoritative value when a run and chapter list overlap', () => {
    expect(resolveGeneratedChapterCount(5, [{ number: 1 }, { number: 4 }])).toBe(5)
  })

  it('never allows a regeneration target before its restart chapter', () => {
    expect(normalizeWorldlineTarget(4, 2)).toBe(4)
    expect(normalizeWorldlineTarget(4, 6)).toBe(6)
  })
})

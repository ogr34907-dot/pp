import { describe, expect, it } from 'vitest'
import { outlineLines, outlineStringLists, outlineText } from './outlinePresentation'

describe('outline presentation', () => {
  it('renders malformed list and object values without calling join on them', () => {
    expect(outlineText(['总纲', { handoff: '进入第二部' }])).toBe('总纲\n进入第二部')
    expect(outlineLines({ first: '雨夜', second: ['旧信', '钟声'] })).toEqual(['雨夜', '旧信', '钟声'])
    expect(outlineStringLists(['伏笔一'])).toEqual({ items: ['伏笔一'] })
  })
})

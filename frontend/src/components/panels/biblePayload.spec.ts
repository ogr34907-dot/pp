import { describe, expect, it } from 'vitest'
import { toBibleApiPayload } from './biblePayload'

describe('toBibleApiPayload', () => {
  it('namespaces generated Bible child IDs with the novel ID', () => {
    const payload = toBibleApiPayload('novel-1786004410714', {
      characters: [
        { name: '沈青', role: '主角', traits: '冷静', arc_note: '成长' },
        { name: '陆远', role: '同伴', traits: '果断', arc_note: '' },
      ],
      locations: [
        { name: '云城', description: '故事起点' },
      ],
      style_notes: '克制的第三人称叙述。',
    })

    expect(payload.characters.map((character) => character.id)).toEqual([
      'novel-1786004410714-char-1',
      'novel-1786004410714-char-2',
    ])
    expect(payload.locations.map((location) => location.id)).toEqual([
      'novel-1786004410714-loc-1',
    ])
    expect(payload.style_notes.map((note) => note.id)).toEqual([
      'novel-1786004410714-style-1',
    ])
  })
})

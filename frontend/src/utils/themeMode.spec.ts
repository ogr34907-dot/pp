import { describe, expect, it } from 'vitest'

import { normalizePlotPilotTheme } from './themeMode'

describe('normalizePlotPilotTheme', () => {
  it('keeps only the explicit light theme light', () => {
    expect(normalizePlotPilotTheme('light')).toBe('light')
  })

  it.each([
    ['dark', 'dark'],
    ['anchor', 'dark'],
    ['black-gold', 'dark'],
    [undefined, 'dark'],
    ['legacy-midnight', 'dark'],
  ] as const)('normalizes %s to the safe dark theme', (value, expected) => {
    expect(normalizePlotPilotTheme(value)).toBe(expected)
  })
})

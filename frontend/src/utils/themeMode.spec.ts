import { describe, expect, it } from 'vitest'

import { normalizePlotPilotTheme } from './themeMode'

describe('normalizePlotPilotTheme', () => {
  it.each([
    ['dark', 'dark'],
    ['anchor', 'dark'],
    ['black-gold', 'dark'],
  ] as const)('migrates the explicit legacy dark value %s to dark', (value, expected) => {
    expect(normalizePlotPilotTheme(value)).toBe(expected)
  })

  it.each([
    ['light', 'light'],
    [undefined, 'light'],
    [null, 'light'],
    ['', 'light'],
    ['legacy-midnight', 'light'],
    ['corrupted-value', 'light'],
  ] as const)('normalizes the missing or unsupported value %s to warm-paper light', (value, expected) => {
    expect(normalizePlotPilotTheme(value)).toBe(expected)
  })
})

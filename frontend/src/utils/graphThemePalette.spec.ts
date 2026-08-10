import { describe, expect, it } from 'vitest'
import { getEditorialGraphPalette } from './graphThemePalette'

describe('getEditorialGraphPalette', () => {
  it('provides literal canvas-safe warm colors for both app themes', () => {
    for (const theme of ['light', 'dark'] as const) {
      const palette = getEditorialGraphPalette(theme)
      const colors = Object.values(palette).flatMap(value =>
        typeof value === 'string' ? [value] : Object.values(value),
      )

      expect(colors.every(color => !color.includes('var('))).toBe(true)
      expect(colors.every(color => /^#|^rgba\(/.test(color))).toBe(true)
    }
  })

  it('keeps primary, secondary, minor, neutral, and subdued graph states distinguishable', () => {
    const palette = getEditorialGraphPalette('light')
    const statePairs = [palette.primary, palette.secondary, palette.minor, palette.neutral, palette.subdued]

    expect(new Set(statePairs.map(state => `${state.background}/${state.border}`)).size).toBe(statePairs.length)
  })
})

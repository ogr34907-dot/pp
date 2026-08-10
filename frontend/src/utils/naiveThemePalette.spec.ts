import { rgba } from 'seemly'
import { describe, expect, it } from 'vitest'

import { getNaiveThemeColorPalette } from './naiveThemePalette'

describe('getNaiveThemeColorPalette', () => {
  it.each([
    ['light', '#A64B2A', '#FFFCF6', '#F4EFE6', '#2C2520', '#E2D8CA'],
    ['dark', '#E28B62', '#2B2420', '#221C19', '#F6EEDF', '#4A3E37'],
  ] as const)(
    'prevents passing a non-parsable CSS variable to Naive UI for the %s theme',
    (theme, primary, surface, canvas, ink, border) => {
      const palette = getNaiveThemeColorPalette(theme)

      expect(palette).toMatchObject({ primary, surface, canvas, ink, border })

      for (const color of Object.values(palette)) {
        expect(color).not.toContain('var(')
        expect(rgba(color)).toHaveLength(4)
      }
    },
  )
})

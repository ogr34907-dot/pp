import { rgba } from 'seemly'
import { describe, expect, it } from 'vitest'

import { contrastRatio } from './colorContrast'
import { getNaiveThemeColorPalette } from './naiveThemePalette'

describe('getNaiveThemeColorPalette', () => {
  it.each([
    [
      'light',
      '#A64B2A',
      '#FFFCF6',
      '#F4EFE6',
      '#2C2520',
      '#E2D8CA',
      'rgba(44, 37, 32, 0.12)',
    ],
    [
      'dark',
      '#E28B62',
      '#2B2420',
      '#221C19',
      '#F6EEDF',
      '#4A3E37',
      'rgba(246, 238, 223, 0.12)',
    ],
  ] as const)(
    'prevents passing a non-parsable CSS variable to Naive UI for the %s theme',
    (theme, primary, surface, canvas, ink, border, divider) => {
      const palette = getNaiveThemeColorPalette(theme)

      expect(palette).toMatchObject({ primary, surface, canvas, ink, border, divider })

      for (const color of Object.values(palette)) {
        expect(color).not.toContain('var(')
        expect(rgba(color)).toHaveLength(4)
      }
    },
  )

  it.each([
    ['light', '#FFFCF6', '#F4EFE6'],
    ['dark', '#2B2420', '#221C19'],
  ] as const)(
    'keeps the normal muted text path at WCAG AA contrast on %s theme surfaces',
    (theme, surface, canvas) => {
      const palette = getNaiveThemeColorPalette(theme)

      expect(contrastRatio(palette.textMuted, surface)).toBeGreaterThanOrEqual(4.5)
      expect(contrastRatio(palette.textMuted, canvas)).toBeGreaterThanOrEqual(4.5)
    },
  )
})

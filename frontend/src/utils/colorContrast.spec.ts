import { describe, expect, it } from 'vitest'

import { contrastRatio } from './colorContrast'

describe('contrastRatio', () => {
  it('calculates the WCAG ratio used to protect normal-size readable text', () => {
    expect(contrastRatio('#000000', '#FFFFFF')).toBe(21)
    expect(contrastRatio('#665B52', '#FFFCF6')).toBeGreaterThanOrEqual(4.5)
  })
})

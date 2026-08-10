import { describe, expect, it } from 'vitest'

import { BRAND } from './brand'

describe('BRAND public contract', () => {
  it('identifies PlotPilot without reintroducing a prohibited public brand field', () => {
    expect(BRAND.productName).toBe('PlotPilot')
    expect(Object.keys(BRAND)).toEqual(['productName', 'tagline', 'descriptor'])
    expect(BRAND).not.toHaveProperty('chineseName')
    expect(BRAND).not.toHaveProperty('displayName')
    expect(BRAND).not.toHaveProperty('team')
    expect(BRAND).not.toHaveProperty('credit')
    expect(BRAND).not.toHaveProperty('douyinLabel')
    expect(BRAND).not.toHaveProperty('douyinUrl')
    expect(BRAND).not.toHaveProperty('liveSchedule')
  })
})

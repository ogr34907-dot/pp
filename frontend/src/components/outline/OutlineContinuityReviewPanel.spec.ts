import { describe, expect, it } from 'vitest'

import componentSource from './OutlineContinuityReviewPanel.vue?raw'

describe('OutlineContinuityReviewPanel contract', () => {
  it('keeps technical blockers separate from narrative acknowledgement', () => {
    expect(componentSource).toContain('technicalBlockers.length > 0')
    expect(componentSource).toContain('type="error"')
    expect(componentSource).toContain("props.status?.current && !technicalBlockers.value.length")
  })

  it('requires a reason for every non-pass acknowledgement', () => {
    expect(componentSource).toContain("decision.value !== '' && decision.value !== 'pass'")
    expect(componentSource).toContain('needsReason && !reason.trim()')
    expect(componentSource).toContain("$emit('acknowledge', { reason: reason.trim() })")
  })

  it('only exposes a suggestion apply action for current unlocked reports', () => {
    expect(componentSource).toContain('props.status?.current')
    expect(componentSource).toContain('props.lockedFields || []')
    expect(componentSource).toContain("$emit('applySuggestion', { suggestionId: item.id })")
  })
})

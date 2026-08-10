import { describe, expect, it } from 'vitest'
import { getCanonicalAftermathPresentation } from './canonicalAftermathGate'

describe('getCanonicalAftermathPresentation', () => {
  it('prioritizes a canonical failure over a ready macro structure gate', () => {
    const presentation = getCanonicalAftermathPresentation({
      autopilot_pause_reason: 'canonical_aftermath_not_ready',
      review_gate: {
        type: 'canonical_aftermath',
        action_label: '重新同步第 8 章',
        can_resume: false,
      },
      macro_structure_ready: true,
    })

    expect(presentation).toMatchObject({
      isFailure: true,
      title: '规范章后同步失败',
      currentChapterAction: {
        intent: 'retry',
        label: '重新同步第 8 章',
        disabled: false,
      },
      resumeAction: {
        intent: 'resume',
        disabled: true,
      },
    })
    expect(presentation.currentChapterAction.label).not.toContain('确认结构')
  })
})

import { describe, expect, it } from 'vitest'

import componentSource from './GenerationModeLauncher.vue?raw'

describe('GenerationModeLauncher start contract', () => {
  it('keeps Continuous on the single start request', () => {
    expect(componentSource).toContain('generationApi.start(props.novelId, mode.value)')
    expect(componentSource).not.toContain('generationApi.runContinuous(props.novelId)')
  })

  it('only generates the first candidate for chapter review mode', () => {
    expect(componentSource).toContain("if (mode.value === 'chapter_review')")
    expect(componentSource).toContain('generationApi.generateNext(props.novelId)')
  })
})

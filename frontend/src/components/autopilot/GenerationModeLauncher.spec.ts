import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const componentSource = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), 'GenerationModeLauncher.vue'),
  'utf8',
)

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

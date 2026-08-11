import { describe, expect, it } from 'vitest'
import {
  clipInvocationStreamPreview,
  formatInvocationElapsedSeconds,
  getOnboardingInvocationPresentation,
} from './onboardingInvocationProgress'

describe('onboarding invocation progress', () => {
  it('describes the real generation lifecycle instead of dimension-level fake progress', () => {
    expect(
      getOnboardingInvocationPresentation({
        task: 'worldbuilding',
        sessionStatus: 'generating',
      }),
    ).toMatchObject({
      phase: 'generating',
      message: 'AI 正在生成完整世界观',
      detail: '五个维度会在校验后统一写入',
    })

    expect(
      getOnboardingInvocationPresentation({
        task: 'characters',
        sessionStatus: 'awaiting_acceptance',
      }),
    ).toMatchObject({ phase: 'validating', message: '正在校验人物设定' })

    expect(
      getOnboardingInvocationPresentation({
        task: 'locations',
        sessionStatus: 'awaiting_commit',
      }),
    ).toMatchObject({ phase: 'committing', message: '正在写入地图与地点' })

    expect(
      getOnboardingInvocationPresentation({
        task: 'plot_outline',
        commitStatus: 'succeeded',
      }),
    ).toMatchObject({ phase: 'completed', message: '剧情总纲已生成' })
  })

  it('keeps stream content real, bounded, and visibly tail-oriented', () => {
    expect(clipInvocationStreamPreview('第一段输出')).toBe('第一段输出')
    expect(clipInvocationStreamPreview('abcdef', 4)).toBe('…cdef')
    expect(clipInvocationStreamPreview('', 4)).toBe('')
  })

  it('formats elapsed time without implying model progress', () => {
    expect(formatInvocationElapsedSeconds(8)).toBe('已等待 8 秒')
    expect(formatInvocationElapsedSeconds(72)).toBe('已等待 1 分 12 秒')
  })
})

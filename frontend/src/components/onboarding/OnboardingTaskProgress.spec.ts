import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { describe, expect, it } from 'vitest'
import OnboardingTaskProgress from './OnboardingTaskProgress.vue'

describe('OnboardingTaskProgress', () => {
  it('renders lifecycle, elapsed time, and only the actual streamed fragment', async () => {
    const app = createSSRApp({
      render: () => h(OnboardingTaskProgress, {
        task: 'worldbuilding',
        phase: 'generating',
        elapsedSeconds: 72,
        streamContent: '真实的服务端增量输出',
      }),
    })

    const html = await renderToString(app)

    expect(html).toContain('role="status"')
    expect(html).toContain('AI 正在生成完整世界观')
    expect(html).toContain('已等待 1 分 12 秒')
    expect(html).toContain('实时生成片段')
    expect(html).toContain('真实的服务端增量输出')
  })

  it('does not pretend text exists before the first persisted stream chunk', async () => {
    const app = createSSRApp({
      render: () => h(OnboardingTaskProgress, {
        task: 'characters',
        phase: 'creating',
        elapsedSeconds: 0,
      }),
    })

    const html = await renderToString(app)

    expect(html).toContain('等待首段输出')
    expect(html).not.toContain('streaming-cursor')
  })
})

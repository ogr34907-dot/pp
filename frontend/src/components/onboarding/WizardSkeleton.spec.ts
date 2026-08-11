import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { describe, expect, it } from 'vitest'
import WizardSkeleton from './WizardSkeleton.vue'

describe('WizardSkeleton invocation mode', () => {
  it('does not represent a one-shot worldbuilding invocation as fake 0/5 dimension progress', async () => {
    const app = createSSRApp({
      render: () => h(WizardSkeleton, {
        type: 'worldbuilding',
        invocationActive: true,
        invocationMessage: '完整设定正在生成，五个维度会在校验后统一写入。',
      }),
    })

    const html = await renderToString(app)

    expect(html).toContain('完整设定生成中')
    expect(html).toContain('统一生成中')
    expect(html).toContain('完整设定正在生成，五个维度会在校验后统一写入。')
    expect(html).not.toContain('0 / 5')
  })
})

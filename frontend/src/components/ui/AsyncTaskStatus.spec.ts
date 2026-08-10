import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { describe, expect, it } from 'vitest'
import AsyncTaskStatus from './AsyncTaskStatus.vue'

describe('AsyncTaskStatus accessibility surface', () => {
  it('renders a failed task as an assertive alert with a disabled recovery button', async () => {
    const app = createSSRApp({
      render: () => h(AsyncTaskStatus, {
        status: 'failed',
        stage: '规范章后同步失败',
        message: '第 64 章的规范记忆同步尚未完成',
        recoveryLabel: '重新同步本章',
        recoveryDisabled: true,
      }),
    })

    const html = await renderToString(app)

    expect(html).toContain('role="alert"')
    expect(html).toContain('aria-live="assertive"')
    expect(html).toContain('需要处理')
    expect(html).toContain('disabled')
  })
})

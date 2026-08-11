import { describe, expect, it } from 'vitest'
import { AUTOPILOT_WORKSPACE_TABS } from './autopilotWorkspaceStore'

describe('AUTOPILOT_WORKSPACE_TABS', () => {
  it('describes the entry tab as candidate writing', () => {
    const cockpit = AUTOPILOT_WORKSPACE_TABS.find(tab => tab.id === 'cockpit')

    expect(cockpit).toMatchObject({
      label: '候选写作',
      short: '候选写作',
      description: '生成候选章并跟踪审稿进度',
    })
  })
})

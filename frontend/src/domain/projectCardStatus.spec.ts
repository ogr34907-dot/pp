import { describe, expect, it } from 'vitest'
import { getProjectCardStatusPresentation } from './projectCardStatus'

describe('getProjectCardStatusPresentation', () => {
  it('shows an explicit terminated state after a manual autopilot termination', () => {
    expect(
      getProjectCardStatusPresentation({
        stage: 'writing',
        autopilotStatus: 'stopped',
        recoveryReason: 'manual_terminate',
      }),
    ).toMatchObject({ key: 'terminated', label: '已终止', tagType: 'default' })
  })

  it('shows an explicit paused state after a manual pause', () => {
    expect(
      getProjectCardStatusPresentation({
        stage: 'writing',
        autopilotStatus: 'stopped',
        recoveryReason: 'manual_pause',
      }),
    ).toMatchObject({ key: 'paused', label: '已暂停', tagType: 'default' })
  })

  it('keeps the lifecycle stage for a normally stopped project', () => {
    expect(
      getProjectCardStatusPresentation({
        stage: 'planning',
        autopilotStatus: 'stopped',
      }),
    ).toMatchObject({ key: 'planning', label: '规划中', tagType: 'info' })
  })

  it('prioritizes an autopilot error over its last lifecycle stage', () => {
    expect(
      getProjectCardStatusPresentation({
        stage: 'writing',
        autopilotStatus: 'error',
      }),
    ).toMatchObject({ key: 'error', label: '异常挂起', tagType: 'error' })
  })
})

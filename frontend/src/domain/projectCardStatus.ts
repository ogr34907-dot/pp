import {
  getNovelStageLabel,
  getNovelStageTagType,
  type NovelStageTagType,
} from './novel'

export type ProjectCardStatusTagType = NovelStageTagType | 'error'

export interface ProjectCardStatusInput {
  stage?: string | null
  autopilotStatus?: string | null
  recoveryReason?: string | null
}

export interface ProjectCardStatusPresentation {
  key: string
  label: string
  tagType: ProjectCardStatusTagType
}

function normalize(value?: string | null): string {
  return String(value || '').trim().toLowerCase()
}

/**
 * 首页书卡要优先反映自动驾驶的明确终态，避免用户终止后仍显示「写作中」。
 * 普通 stopped 没有终止原因时仍按小说生命周期展示，兼容未启动的新书。
 */
export function getProjectCardStatusPresentation(
  input: ProjectCardStatusInput,
): ProjectCardStatusPresentation {
  const stage = String(input.stage || '').trim() || 'planning'
  const autopilotStatus = normalize(input.autopilotStatus)
  const recoveryReason = normalize(input.recoveryReason)

  if (autopilotStatus === 'error') {
    return { key: 'error', label: '异常挂起', tagType: 'error' }
  }

  if (autopilotStatus === 'stopped' && recoveryReason === 'manual_terminate') {
    return { key: 'terminated', label: '已终止', tagType: 'default' }
  }

  if (autopilotStatus === 'stopped' && recoveryReason === 'manual_pause') {
    return { key: 'paused', label: '已暂停', tagType: 'default' }
  }

  return {
    key: stage,
    label: getNovelStageLabel(stage),
    tagType: getNovelStageTagType(stage),
  }
}

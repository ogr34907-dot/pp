export type OnboardingInvocationTask =
  | 'worldbuilding'
  | 'characters'
  | 'locations'
  | 'plot_outline'

export type OnboardingInvocationPhase =
  | 'creating'
  | 'generating'
  | 'validating'
  | 'committing'
  | 'completed'
  | 'failed'

export interface OnboardingInvocationProgressInput {
  task: OnboardingInvocationTask
  sessionStatus?: string | null
  commitStatus?: string | null
}

export interface OnboardingInvocationPresentation {
  phase: OnboardingInvocationPhase
  message: string
  detail: string
  isTerminal: boolean
}

type TaskCopy = Record<OnboardingInvocationPhase, Omit<OnboardingInvocationPresentation, 'phase'>>

const TASK_COPY: Record<OnboardingInvocationTask, TaskCopy> = {
  worldbuilding: {
    creating: { message: '正在准备世界观任务', detail: '正在读取本书的基础设定', isTerminal: false },
    generating: { message: 'AI 正在生成完整世界观', detail: '五个维度会在校验后统一写入', isTerminal: false },
    validating: { message: '正在校验世界观设定', detail: '正在检查结构与字段完整性', isTerminal: false },
    committing: { message: '正在写入文风与世界观', detail: '正在保存可编辑的设定结果', isTerminal: false },
    completed: { message: '文风与世界观已生成', detail: '设定已可编辑', isTerminal: true },
    failed: { message: '世界观生成需要处理', detail: '请查看错误信息后重试', isTerminal: true },
  },
  characters: {
    creating: { message: '正在准备人物任务', detail: '正在读取已确认的世界观设定', isTerminal: false },
    generating: { message: 'AI 正在生成人物', detail: '角色设定会在校验后统一呈现', isTerminal: false },
    validating: { message: '正在校验人物设定', detail: '正在检查人物关系与写作锚点', isTerminal: false },
    committing: { message: '正在写入人物设定', detail: '正在保存可编辑的角色结果', isTerminal: false },
    completed: { message: '人物设定已生成', detail: '角色已可编辑', isTerminal: true },
    failed: { message: '人物生成需要处理', detail: '请查看错误信息后重试', isTerminal: true },
  },
  locations: {
    creating: { message: '正在准备地图任务', detail: '正在读取世界观与人物设定', isTerminal: false },
    generating: { message: 'AI 正在生成地图与地点', detail: '地点设定会在校验后统一呈现', isTerminal: false },
    validating: { message: '正在校验地图与地点', detail: '正在检查地点层级与描述完整性', isTerminal: false },
    committing: { message: '正在写入地图与地点', detail: '正在保存可编辑的地点结果', isTerminal: false },
    completed: { message: '地图与地点已生成', detail: '地点已可编辑', isTerminal: true },
    failed: { message: '地图生成需要处理', detail: '请查看错误信息后重试', isTerminal: true },
  },
  plot_outline: {
    creating: { message: '正在准备剧情总纲任务', detail: '正在汇总已确认的设定', isTerminal: false },
    generating: { message: 'AI 正在生成剧情总纲', detail: '正在推演主线、阶段与核心冲突', isTerminal: false },
    validating: { message: '正在校验剧情总纲', detail: '正在检查结构和章节范围', isTerminal: false },
    committing: { message: '正在写入剧情总纲', detail: '正在保存可编辑的主线规划', isTerminal: false },
    completed: { message: '剧情总纲已生成', detail: '主线规划已可编辑', isTerminal: true },
    failed: { message: '剧情总纲生成需要处理', detail: '请查看错误信息后重试', isTerminal: true },
  },
}

function normalize(value?: string | null): string {
  return String(value || '').trim().toLowerCase()
}

export function getOnboardingInvocationPhase(
  input: Omit<OnboardingInvocationProgressInput, 'task'>,
): OnboardingInvocationPhase {
  const sessionStatus = normalize(input.sessionStatus)
  const commitStatus = normalize(input.commitStatus)

  if (commitStatus === 'succeeded' || sessionStatus === 'completed') return 'completed'
  if (
    commitStatus === 'failed' ||
    sessionStatus === 'failed' ||
    sessionStatus === 'cancelled' ||
    sessionStatus === 'blocked'
  ) return 'failed'
  if (
    commitStatus === 'running' ||
    sessionStatus === 'awaiting_commit' ||
    sessionStatus === 'committing'
  ) return 'committing'
  if (sessionStatus === 'awaiting_acceptance') return 'validating'
  if (sessionStatus === 'generating' || sessionStatus === 'streaming') return 'generating'
  return 'creating'
}

export function getOnboardingTaskPresentation(
  task: OnboardingInvocationTask,
  phase: OnboardingInvocationPhase,
): OnboardingInvocationPresentation {
  return { phase, ...TASK_COPY[task][phase] }
}

export function getOnboardingInvocationPresentation(
  input: OnboardingInvocationProgressInput,
): OnboardingInvocationPresentation {
  return getOnboardingTaskPresentation(
    input.task,
    getOnboardingInvocationPhase(input),
  )
}

/** 只显示服务端已持久化的真实增量文本；超长时保留最新尾段，避免向导无限撑高。 */
export function clipInvocationStreamPreview(content?: string | null, maxLength = 1200): string {
  const text = String(content || '')
  const limit = Math.max(0, Math.floor(maxLength))
  if (!text || limit === 0) return ''
  return text.length > limit ? `…${text.slice(-limit)}` : text
}

/** 这是等待时间，不是模型完成百分比。 */
export function formatInvocationElapsedSeconds(seconds: number): string {
  const total = Math.max(0, Math.floor(Number(seconds) || 0))
  if (total < 60) return `已等待 ${total} 秒`
  return `已等待 ${Math.floor(total / 60)} 分 ${total % 60} 秒`
}

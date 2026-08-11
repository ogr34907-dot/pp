import type { GenerationPresentationTone } from './generationPresentation'

export type CandidateGenerationStepState = 'pending' | 'active' | 'complete' | 'attention' | 'failed'

export interface CandidateGenerationProgressInput {
  state?: string | null
  run_mode?: string | null
  target_chapters?: number | null
  current_formal_chapter?: number | null
  current_candidate_chapter?: number | null
  canonical_sync_status?: string | null
  last_error?: string | null
  candidate?: { status?: string | null; chapter_number?: number | null } | null
}

export interface CandidateGenerationProgressStep {
  key: 'draft' | 'audit' | 'review' | 'commit' | 'sync'
  label: string
  detail: string
  state: CandidateGenerationStepState
}

export interface CandidateGenerationProgress {
  label: string
  detail: string
  tone: GenerationPresentationTone
  isActive: boolean
  activeStep: number
  formalProgress: number
  formalChapters: number
  targetChapters: number
  candidateChapter: number | null
  modeLabel: string
  steps: CandidateGenerationProgressStep[]
}

const stepBlueprint: Array<Pick<CandidateGenerationProgressStep, 'key' | 'label' | 'detail'>> = [
  { key: 'draft', label: '候选生成', detail: '正文仅保存在候选稿中' },
  { key: 'audit', label: '机器审校', detail: '核对大纲、连续性与提交清单' },
  { key: 'review', label: '作者审核', detail: '确认正文和提交清单后才可入库' },
  { key: 'commit', label: '正式写入', detail: '将作者确认的版本写入书稿' },
  { key: 'sync', label: '规范同步', detail: '更新事实、向量和长期记忆' },
]

function clean(value?: string | null): string {
  return String(value || '').trim().toLowerCase()
}

function asNonNegativeInt(value?: number | null): number {
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0
}

function candidateChapter(input: CandidateGenerationProgressInput): number | null {
  const value = input.current_candidate_chapter ?? input.candidate?.chapter_number
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(1, Math.floor(value)) : null
}

function makeSteps(states: CandidateGenerationStepState[]): CandidateGenerationProgressStep[] {
  return stepBlueprint.map((step, index) => ({ ...step, state: states[index] || 'pending' }))
}

function activeResult(
  states: CandidateGenerationStepState[],
  activeStep: number,
  label: string,
  detail: string,
  tone: GenerationPresentationTone = 'brand',
): Pick<CandidateGenerationProgress, 'steps' | 'activeStep' | 'label' | 'detail' | 'tone' | 'isActive'> {
  return { steps: makeSteps(states), activeStep, label, detail, tone, isActive: tone === 'brand' }
}

/**
 * Turn the server-owned candidate workflow into a compact, stable five-stage
 * progress model. The browser may render it, but it must not invent state.
 */
export function getCandidateGenerationProgress(
  input: CandidateGenerationProgressInput | null | undefined,
): CandidateGenerationProgress {
  const formalChapters = asNonNegativeInt(input?.current_formal_chapter)
  const targetChapters = asNonNegativeInt(input?.target_chapters)
  const formalProgress = targetChapters > 0
    ? Math.min(100, Math.round((formalChapters / targetChapters) * 100))
    : 0
  const candidate = input ? candidateChapter(input) : null
  const candidatePrefix = candidate ? `第 ${candidate} 章` : '当前候选章'
  const modeLabel = clean(input?.run_mode) === 'chapter_review' ? '逐章人工审核' : '连续自动驾驶'

  let result: Pick<CandidateGenerationProgress, 'steps' | 'activeStep' | 'label' | 'detail' | 'tone' | 'isActive'>
  if (!input) {
    result = activeResult(
      ['pending', 'pending', 'pending', 'pending', 'pending'],
      0,
      '等待开始',
      '发布并同步五级大纲后，选择本次写作模式。',
      'neutral',
    )
  } else if (clean(input.canonical_sync_status) === 'failed') {
    result = activeResult(
      ['complete', 'complete', 'complete', 'complete', 'failed'],
      4,
      `${candidatePrefix}规范同步失败`,
      String(input.last_error || '正式章节已写入；完成规范同步前不会生成下一章。'),
      'error',
    )
  } else if (clean(input.canonical_sync_status) === 'rebuilding') {
    result = activeResult(
      ['complete', 'complete', 'complete', 'complete', 'active'],
      4,
      '世界线正在重建规范事实',
      '重建结束前，新世界线不会开始生成下一章。',
    )
  } else {
    const state = clean(input.state)
    const candidateState = clean(input.candidate?.status)
    if (candidateState === 'streaming' || candidateState === 'regenerating') {
      result = activeResult(
        ['active', 'pending', 'pending', 'pending', 'pending'],
        0,
        `${candidatePrefix}候选生成中`,
        '正文正在生成，尚未进入正式书稿。',
      )
    } else if (candidateState === 'auditing') {
      result = activeResult(
        ['complete', 'active', 'pending', 'pending', 'pending'],
        1,
        `${candidatePrefix}机器审校中`,
        '正在核对大纲契约、连续性与提交清单。',
      )
    } else if (candidateState === 'awaiting_review' || state === 'waiting_review') {
      result = activeResult(
        ['complete', 'complete', 'attention', 'pending', 'pending'],
        2,
        `${candidatePrefix}待作者审核`,
        '作者确认前不会生成下一章，也不会消耗下一章 Token。',
        'warning',
      )
    } else if (candidateState === 'stale') {
      result = activeResult(
        ['complete', 'attention', 'pending', 'pending', 'pending'],
        1,
        `${candidatePrefix}审校结果已过期`,
        '正文或提交清单已修改，请重新审校后再正式提交。',
        'warning',
      )
    } else if (candidateState === 'committing') {
      result = activeResult(
        ['complete', 'complete', 'complete', 'active', 'pending'],
        3,
        `${candidatePrefix}正在正式写入`,
        '仅作者确认的正文与提交清单会进入正式书稿。',
      )
    } else if (candidateState === 'syncing' || clean(input.canonical_sync_status) === 'syncing') {
      result = activeResult(
        ['complete', 'complete', 'complete', 'complete', 'active'],
        4,
        `${candidatePrefix}正在规范同步`,
        '正在更新章节事实、向量与长期记忆。',
      )
    } else if (candidateState === 'committed' || state === 'completed') {
      result = activeResult(
        ['complete', 'complete', 'complete', 'complete', 'complete'],
        4,
        state === 'completed' ? '已完成目标章节' : `${candidatePrefix}已完成提交`,
        state === 'completed' ? '所有正式章节与规范记忆均已就绪。' : '该章节已完成正式写入与规范同步。',
        'success',
      )
    } else if (candidateState === 'failed') {
      result = activeResult(
        ['failed', 'pending', 'pending', 'pending', 'pending'],
        0,
        `${candidatePrefix}生成失败`,
        String(input.last_error || '请检查失败原因后重新生成当前候选章。'),
        'error',
      )
    } else if (candidateState === 'cancelled' || candidateState === 'rejected' || state === 'stopped') {
      result = activeResult(
        ['attention', 'pending', 'pending', 'pending', 'pending'],
        0,
        '本次生成已停止',
        '未通过的候选稿不会进入正式书稿或后续上下文。',
        'neutral',
      )
    } else if (state === 'paused') {
      result = activeResult(
        ['attention', 'pending', 'pending', 'pending', 'pending'],
        0,
        '生成已暂停',
        String(input.last_error || '当前进度已持久化，可在确认状态后继续。'),
        'warning',
      )
    } else {
      result = activeResult(
        ['active', 'pending', 'pending', 'pending', 'pending'],
        0,
        `${candidatePrefix}正在准备生成`,
        '正在读取已发布的五级大纲并建立候选稿。',
      )
    }
  }

  return {
    ...result,
    formalProgress,
    formalChapters,
    targetChapters,
    candidateChapter: candidate,
    modeLabel,
  }
}

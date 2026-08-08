import { apiClient } from './config'

export interface NarrativeContractDTO {
  novel_id: string
  title_promise: string
  core_question: string
  theme_anchors: string[]
  forbidden_early_payoffs: string[]
  reveal_budget: Record<string, unknown>
  updated_at: string
}

export interface CanonicalStorylineDTO {
  canonical_id: string
  novel_id: string
  canonical_key: string
  title: string
  aliases: string[]
  goal: string
  conflict: string
  span: Record<string, number | null>
  promise_tags: string[]
  status: string
  source_storyline_ids: string[]
  updated_at: string
}

export interface ChapterNarrativeBudgetDTO {
  novel_id: string
  chapter_number: number
  max_new_storylines: number
  max_debt_closures: number
  allowed_reveal_level: string
  must_serve_promise_tags: string[]
  carry_over_debt_ids: string[]
  notes: string[]
}

export interface GovernanceIssueDTO {
  code: string
  severity: string
  title: string
  detail: string
  evidence: string[]
  suggestion: string
}

export interface GovernanceReportDTO {
  report_id: string
  novel_id: string
  chapter_number: number
  severity: string
  promise_hit_rate: number
  issues: GovernanceIssueDTO[]
  budget_patch: Record<string, unknown>
  should_pause_autopilot: boolean
  created_at: string
  review_status: string
}

export interface GovernanceStateDTO {
  contract: NarrativeContractDTO
  canonical_storylines: CanonicalStorylineDTO[]
  open_debts: Array<Record<string, unknown>>
  latest_report: GovernanceReportDTO | null
  chapter_budget_preview: ChapterNarrativeBudgetDTO
  hierarchy_alignment?: HierarchyAlignmentReportDTO | null
}

export interface HierarchyAlignmentViolationDTO {
  scope: string
  severity: string
  expected: string
  actual: string
  evidence_refs: string[]
  repair: string
}

export interface HierarchyAlignmentReportDTO {
  decision: 'pass' | 'review' | 'block' | 'evidence_degraded' | 'overridden' | string
  confidence: number
  candidate_digest: string
  snapshot_digest: string
  violations: HierarchyAlignmentViolationDTO[]
  repair_plan: string[]
  evidence_refs: string[]
  evidence_degraded: boolean
  overridden: boolean
  override_reason?: string | null
}

export interface HierarchyReplanPreviewDTO {
  novel_id: string
  chapter_id: string
  would_write: false
  mutations: Array<Record<string, unknown>>
  alignment_report: HierarchyAlignmentReportDTO
  repair_plan: string[]
}

export function getGovernanceState(novelId: string) {
  return apiClient.get<GovernanceStateDTO>(`/novels/${novelId}/governance/state`)
}

export function updateGovernanceContract(novelId: string, payload: Partial<NarrativeContractDTO>) {
  return apiClient.post<NarrativeContractDTO>(`/novels/${novelId}/governance/contract`, payload)
}

export function previewGovernanceBudget(novelId: string, chapterNumber?: number) {
  return apiClient.post<{ budget: ChapterNarrativeBudgetDTO; context_request: Record<string, unknown> }>(
    `/novels/${novelId}/governance/chapter-budget/preview`,
    { chapter_number: chapterNumber ?? null },
  )
}

export function mergeGovernanceStorylines(
  novelId: string,
  payload: { source_ids: string[]; target_id?: string; title?: string; aliases?: string[]; promise_tags?: string[] },
) {
  return apiClient.post<CanonicalStorylineDTO>(`/novels/${novelId}/governance/storylines/merge`, payload)
}

export function applyGovernanceReviewAction(
  novelId: string,
  payload: { report_id: string; action: string; patch?: Record<string, unknown> },
) {
  return apiClient.post<{ report_id: string; status: string }>(
    `/novels/${novelId}/governance/review-action`,
    payload,
  )
}

export function previewHierarchyAlignment(
  novelId: string,
  payload: { chapter_id: string; candidate: Record<string, unknown> },
) {
  return apiClient.post<HierarchyAlignmentReportDTO>(
    `/novels/${novelId}/governance/hierarchy/preview`, payload,
  )
}

export function overrideHierarchyAlignment(
  novelId: string,
  payload: { candidate_digest: string; reason: string },
) {
  return apiClient.post<HierarchyAlignmentReportDTO>(
    `/novels/${novelId}/governance/hierarchy/override`, payload,
  )
}

export function previewHierarchyReplan(
  novelId: string,
  payload: { chapter_id: string; candidate: Record<string, unknown> },
) {
  return apiClient.post<HierarchyReplanPreviewDTO>(
    `/novels/${novelId}/governance/hierarchy/replan/preview`, payload,
  )
}

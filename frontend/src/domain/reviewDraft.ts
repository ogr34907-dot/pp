export interface ReviewDraft {
  content: string
  feedback: string
  chapterSummary: string
  eventsText: string
  handoffText: string
}

export function isReviewDraftDirty(draft: ReviewDraft, saved: ReviewDraft): boolean {
  return draft.content !== saved.content ||
    draft.feedback !== saved.feedback ||
    draft.chapterSummary !== saved.chapterSummary ||
    draft.eventsText !== saved.eventsText ||
    draft.handoffText !== saved.handoffText
}

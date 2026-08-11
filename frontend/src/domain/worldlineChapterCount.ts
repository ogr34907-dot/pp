export function resolveGeneratedChapterCount(
  currentFormalChapter: number | null | undefined,
  chapters: ReadonlyArray<{ number?: number | null }> = [],
): number {
  const chapterHead = chapters.reduce(
    (head, chapter) => Math.max(head, Number(chapter.number) || 0),
    0,
  )
  return Math.max(Number(currentFormalChapter) || 0, chapterHead)
}

export function normalizeWorldlineTarget(startChapter: number, targetChapters: number): number {
  return Math.max(1, Math.floor(startChapter) || 1, Math.floor(targetChapters) || 1)
}

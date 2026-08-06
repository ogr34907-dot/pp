export type AutopilotStartConfig = {
  target_chapters: number
  target_words_per_chapter: number
  max_auto_chapters: number
  auto_approve_mode: boolean
}

const MIN_PROTECTION_LIMIT = 1
const MAX_PROTECTION_LIMIT = 9999

/** Keep the run cap independent from the manuscript's planned total. */
export function normalizeAutopilotStartConfig(
  config: AutopilotStartConfig,
): AutopilotStartConfig {
  const parsedLimit = Number(config.max_auto_chapters)
  const max_auto_chapters = Number.isFinite(parsedLimit)
    ? Math.min(MAX_PROTECTION_LIMIT, Math.max(MIN_PROTECTION_LIMIT, Math.trunc(parsedLimit)))
    : MIN_PROTECTION_LIMIT

  return {
    ...config,
    max_auto_chapters,
  }
}

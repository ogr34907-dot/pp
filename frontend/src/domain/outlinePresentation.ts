export function outlineText(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value.trim()
  if (Array.isArray(value)) return value.map(outlineText).filter(Boolean).join('\n')
  if (typeof value === 'object') return Object.values(value).map(outlineText).filter(Boolean).join('\n')
  return String(value).trim()
}

export function outlineLines(value: unknown): string[] {
  if (Array.isArray(value)) return value.map(outlineText).filter(Boolean)
  return outlineText(value).split(/\r?\n/).map(item => item.trim()).filter(Boolean)
}

export function outlineStringLists(value: unknown): Record<string, string[]> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    const items = outlineLines(value)
    return items.length ? { items } : {}
  }
  return Object.fromEntries(
    Object.entries(value).map(([key, items]) => [key, outlineLines(items)]).filter(([, items]) => items.length),
  )
}

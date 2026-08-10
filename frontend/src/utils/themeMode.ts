export type PlotPilotTheme = 'light' | 'dark'

export function normalizePlotPilotTheme(value: unknown): PlotPilotTheme {
  return value === 'dark' || value === 'anchor' || value === 'black-gold'
    ? 'dark'
    : 'light'
}

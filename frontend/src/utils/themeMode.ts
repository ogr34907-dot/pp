export type PlotPilotTheme = 'light' | 'dark'

export function normalizePlotPilotTheme(value: unknown): PlotPilotTheme {
  return value === 'light' ? 'light' : 'dark'
}

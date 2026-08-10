export type EditorialGraphState = {
  background: string
  border: string
}

export type EditorialGraphPalette = {
  primary: EditorialGraphState
  secondary: EditorialGraphState
  minor: EditorialGraphState
  neutral: EditorialGraphState
  subdued: EditorialGraphState
  tooltipBackground: string
  tooltipBorder: string
  tooltipText: string
}

const LIGHT: EditorialGraphPalette = {
  primary: { background: '#F2D5C5', border: '#A64B2A' },
  secondary: { background: '#F0DDB7', border: '#9A7134' },
  minor: { background: '#E8DCCB', border: '#8B7F75' },
  neutral: { background: '#EEE6DA', border: '#C9BBAA' },
  subdued: { background: '#F4EFE6', border: '#E2D8CA' },
  tooltipBackground: '#FFFCF6',
  tooltipBorder: '#C9BBAA',
  tooltipText: '#2C2520',
}

const DARK: EditorialGraphPalette = {
  primary: { background: '#4A2D24', border: '#E28B62' },
  secondary: { background: '#443722', border: '#D7B26A' },
  minor: { background: '#3B312C', border: '#AA9C90' },
  neutral: { background: '#251F1C', border: '#66554A' },
  subdued: { background: '#221C19', border: '#4A3E37' },
  tooltipBackground: '#2B2420',
  tooltipBorder: '#66554A',
  tooltipText: '#F6EEDF',
}

export function getEditorialGraphPalette(theme: 'light' | 'dark'): EditorialGraphPalette {
  return theme === 'dark' ? DARK : LIGHT
}

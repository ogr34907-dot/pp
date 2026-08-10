export type NaiveThemeColorPalette = {
  primary: string
  primaryHover: string
  primaryPressed: string
  primarySuppl: string
  canvas: string
  ink: string
  textSecondary: string
  textMuted: string
  border: string
  divider: string
  surface: string
  surfaceRaised: string
  surfaceSubtle: string
  input: string
  focus: string
  focusSoft: string
}

const LIGHT_PALETTE: NaiveThemeColorPalette = {
  primary: '#A64B2A',
  primaryHover: '#8D3D21',
  primaryPressed: '#76311A',
  primarySuppl: '#BE6A49',
  canvas: '#F4EFE6',
  ink: '#2C2520',
  textSecondary: '#665B52',
  textMuted: '#8B7F75',
  border: '#E2D8CA',
  divider: '#E2D8CA',
  surface: '#FFFCF6',
  surfaceRaised: '#FFFCF6',
  surfaceSubtle: '#EEE6DA',
  input: '#FFFCF6',
  focus: '#A64B2A',
  focusSoft: 'rgba(166, 75, 42, 0.18)',
}

const DARK_PALETTE: NaiveThemeColorPalette = {
  primary: '#E28B62',
  primaryHover: '#EDA17C',
  primaryPressed: '#C9704B',
  primarySuppl: '#F2B496',
  canvas: '#221C19',
  ink: '#F6EEDF',
  textSecondary: '#D3C5B6',
  textMuted: '#AA9C90',
  border: '#4A3E37',
  divider: '#4A3E37',
  surface: '#2B2420',
  surfaceRaised: '#2B2420',
  surfaceSubtle: '#251F1C',
  input: '#2B2420',
  focus: '#E28B62',
  focusSoft: 'rgba(226, 139, 98, 0.22)',
}

export function getNaiveThemeColorPalette(theme: 'light' | 'dark'): NaiveThemeColorPalette {
  return theme === 'dark' ? DARK_PALETTE : LIGHT_PALETTE
}

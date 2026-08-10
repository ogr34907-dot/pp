type Rgb = readonly [number, number, number]

function parseOpaqueHex(color: string): Rgb {
  const value = color.startsWith('#') ? color.slice(1) : color

  if (!/^[\da-f]{6}$/i.test(value)) {
    throw new Error(`Expected an opaque six-digit hex color, received: ${color}`)
  }

  return [
    Number.parseInt(value.slice(0, 2), 16),
    Number.parseInt(value.slice(2, 4), 16),
    Number.parseInt(value.slice(4, 6), 16),
  ]
}

function relativeLuminance([red, green, blue]: Rgb): number {
  const linearChannel = (channel: number) => {
    const normalized = channel / 255
    return normalized <= 0.04045
      ? normalized / 12.92
      : ((normalized + 0.055) / 1.055) ** 2.4
  }

  return (
    0.2126 * linearChannel(red) +
    0.7152 * linearChannel(green) +
    0.0722 * linearChannel(blue)
  )
}

export function contrastRatio(foreground: string, background: string): number {
  const foregroundLuminance = relativeLuminance(parseOpaqueHex(foreground))
  const backgroundLuminance = relativeLuminance(parseOpaqueHex(background))
  const [lighter, darker] = [foregroundLuminance, backgroundLuminance].sort((a, b) => b - a)

  return (lighter + 0.05) / (darker + 0.05)
}

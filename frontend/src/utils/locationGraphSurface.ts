export type LocationGraphSurface = 'loading' | 'empty' | 'graph'

export function resolveLocationGraphSurface(input: {
  loading: boolean
  nodeCount: number
}): LocationGraphSurface {
  if (input.nodeCount > 0) return 'graph'
  return input.loading ? 'loading' : 'empty'
}

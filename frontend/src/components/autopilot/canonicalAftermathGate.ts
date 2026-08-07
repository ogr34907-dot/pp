export interface CanonicalAftermathPresentation {
  isFailure: boolean
  title: string
  actionLabel: string
  canResume: boolean
}

export function getCanonicalAftermathPresentation(
  status: Record<string, any> | null | undefined,
): CanonicalAftermathPresentation {
  const gate = status?.review_gate
  const isFailure = String(status?.autopilot_pause_reason || '') === 'canonical_aftermath_not_ready'
    || String(gate?.type || '') === 'canonical_aftermath'

  if (!isFailure) {
    return {
      isFailure: false,
      title: '',
      actionLabel: '',
      canResume: true,
    }
  }

  return {
    isFailure: true,
    title: '规范章后同步失败',
    actionLabel: String(gate?.action_label || '重新同步本章'),
    canResume: false,
  }
}

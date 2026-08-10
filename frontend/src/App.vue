<script setup lang="ts">
import { computed } from 'vue'
import { NConfigProvider, NMessageProvider, NDialogProvider, zhCN, dateZhCN, darkTheme } from 'naive-ui'
import AppSettingsModal from './components/settings/AppSettingsModal.vue'
import AIInvocationReviewPanel from './components/ai-invocation/AIInvocationReviewPanel.vue'
import { featureFlags } from './config/features'
import type { GlobalThemeOverrides } from 'naive-ui'
import { useThemeStore } from './stores/themeStore'
import { useFontSizeStore, scaledUiPx, type FontSizePreset } from './stores/fontSizeStore'
import { NAIVE_DENSITY_BASE } from './design/layoutDensity'

const themeStore = useThemeStore()
const fontSizeStore = useFontSizeStore()

const naiveTheme = computed(() =>
  themeStore.isDark ? darkTheme : undefined
)

/** Naive UI 形体：随字体档位缩放，基准见 design/layoutDensity */
function naiveShapeOverrides(fz: FontSizePreset): GlobalThemeOverrides {
  const r = scaledUiPx(NAIVE_DENSITY_BASE.borderRadius, fz)
  const rs = scaledUiPx(NAIVE_DENSITY_BASE.borderRadiusSmall, fz)
  const cr = scaledUiPx(NAIVE_DENSITY_BASE.cardBorderRadius, fz)
  const sb = scaledUiPx(NAIVE_DENSITY_BASE.scrollbarWidth, fz)
  return {
    common: {
      borderRadius: r,
      borderRadiusSmall: rs,
      fontSize: scaledUiPx(NAIVE_DENSITY_BASE.fontSize, fz),
      fontSizeMedium: scaledUiPx(NAIVE_DENSITY_BASE.fontSizeMedium, fz),
      lineHeight: NAIVE_DENSITY_BASE.lineHeight,
      heightMedium: scaledUiPx(NAIVE_DENSITY_BASE.heightMedium, fz),
    },
    Card: {
      borderRadius: cr,
      paddingMedium: scaledUiPx(NAIVE_DENSITY_BASE.cardPaddingMedium, fz),
    },
    Button: { borderRadiusMedium: r },
    Input: { borderRadius: r },
    Scrollbar: { width: sb, height: sb, borderRadius: scaledUiPx(3, fz) },
    DataTable: { borderRadius: r, thFontWeight: '600' },
    Tag: { borderRadius: scaledUiPx(5, fz) },
    Progress: {
      railBorderRadius: scaledUiPx(3, fz),
      fillBorderRadius: scaledUiPx(3, fz),
    },
    Drawer: { bodyPadding: '0' },
    Alert: { border: 'none' },
  }
}

// ─── 颜色 + 字号档位是动态的，量少性能好 ─────────────────────────────────────
const themeOverrides = computed<GlobalThemeOverrides>(() => {
  const fz = fontSizeStore.preset

  const shape = naiveShapeOverrides(fz)
  return {
    ...shape,
    common: {
      ...shape.common,
      primaryColor:        'var(--color-brand)',
      primaryColorHover:   'var(--color-brand-hover)',
      primaryColorPressed: 'var(--color-brand-pressed)',
      primaryColorSuppl:   'var(--color-brand-suppl)',
      bodyColor:           'var(--app-page-bg)',
      textColor1:          'var(--app-text-primary)',
      textColor2:          'var(--app-text-secondary)',
      textColor3:          'var(--app-text-muted)',
      borderColor:         'var(--app-border)',
      dividerColor:        'var(--app-divider)',
      cardColor:           'var(--app-surface)',
      modalColor:          'var(--app-surface)',
      popoverColor:        'var(--app-surface-raised)',
      tableColor:          'var(--app-surface)',
      tableColorStriped:   'var(--app-surface-subtle)',
      tableColorHover:     'var(--app-surface-raised)',
      tableHeaderColor:    'var(--app-surface)',
      inputColor:          'var(--app-input-bg)',
      focusColor:          'var(--color-focus-soft)',
    },
    Select: {
      peers: {
        InternalSelection: {
          color:        'var(--app-input-bg)',
          borderActive: 'var(--color-focus)',
          borderFocus:  'var(--color-focus)',
        },
      },
    },
    Drawer: { ...shape.Drawer, color: 'var(--app-surface-subtle)' },
    Tabs: {
      tabTextColorActiveLine: 'var(--color-brand)',
      tabTextColorHoverLine:  'var(--app-text-secondary)',
      barColor:               'var(--color-brand)',
    },
    Switch: { railColorActive: 'var(--color-brand)' },
    Alert:  { ...shape.Alert, color: 'var(--app-surface)' },
    Form:   { labelTextColorTop: 'var(--app-text-secondary)' },
  }
})
</script>

<template>
  <n-config-provider
    :locale="zhCN"
    :date-locale="dateZhCN"
    :theme="naiveTheme"
    :theme-overrides="themeOverrides"
  >
    <n-message-provider>
      <n-dialog-provider>
        <router-view v-slot="{ Component }">
          <transition name="app-fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
        <AIInvocationReviewPanel v-if="featureFlags.aiInvocationDebug" />
        <AppSettingsModal />
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<style>
.app-fade-enter-active,
.app-fade-leave-active {
  transition: opacity 0.2s ease, transform 0.2s ease;
}
.app-fade-enter-from {
  opacity: 0;
  transform: translateY(6px);
}
.app-fade-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
</style>

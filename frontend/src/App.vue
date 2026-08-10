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
import { getNaiveThemeColorPalette } from './utils/naiveThemePalette'

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
  const palette = getNaiveThemeColorPalette(themeStore.effectiveTheme)

  const shape = naiveShapeOverrides(fz)
  return {
    ...shape,
    common: {
      ...shape.common,
      primaryColor:        palette.primary,
      primaryColorHover:   palette.primaryHover,
      primaryColorPressed: palette.primaryPressed,
      primaryColorSuppl:   palette.primarySuppl,
      bodyColor:           palette.canvas,
      textColor1:          palette.ink,
      textColor2:          palette.textSecondary,
      textColor3:          palette.textMuted,
      borderColor:         palette.border,
      dividerColor:        palette.divider,
      cardColor:           palette.surface,
      modalColor:          palette.surface,
      popoverColor:        palette.surfaceRaised,
      tableColor:          palette.surface,
      tableColorStriped:   palette.surfaceSubtle,
      tableColorHover:     palette.surfaceRaised,
      tableHeaderColor:    palette.surface,
      inputColor:          palette.input,
      focusColor:          palette.focusSoft,
    },
    Select: {
      peers: {
        InternalSelection: {
          color:        palette.input,
          borderActive: palette.focus,
          borderFocus:  palette.focus,
        },
      },
    },
    Drawer: { ...shape.Drawer, color: palette.surfaceSubtle },
    Tabs: {
      tabTextColorActiveLine: palette.primary,
      tabTextColorHoverLine:  palette.textSecondary,
      barColor:               palette.primary,
    },
    Switch: { railColorActive: palette.primary },
    Alert:  { ...shape.Alert, color: palette.surface },
    Form:   { labelTextColorTop: palette.textSecondary },
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

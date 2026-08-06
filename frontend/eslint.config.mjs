import js from '@eslint/js'
import vue from 'eslint-plugin-vue'
import globals from 'globals'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  {
    ignores: [
      'dist/**',
      'node_modules/**',
      'scripts/**',
      'src-tauri/**',
    ],
  },
  {
    files: ['src/**/*.{js,mjs,cjs,ts,tsx,vue}'],
    languageOptions: {
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...vue.configs['flat/recommended'],
  {
    files: ['src/**/*.vue'],
    languageOptions: {
      // Keep vue-eslint-parser for templates while delegating <script lang="ts">.
      parserOptions: {
        parser: tseslint.parser,
      },
    },
  },
  {
    files: ['src/**/*.d.ts'],
    rules: {
      // Vite's canonical Vue module declaration intentionally uses empty generics.
      '@typescript-eslint/no-empty-object-type': 'off',
    },
  },
  {
    rules: {
      // Existing data-transfer boundaries intentionally use `any` in a few places.
      '@typescript-eslint/no-explicit-any': 'off',
      // Keep pre-existing unused declarations visible without turning this audit into a broad cleanup.
      '@typescript-eslint/no-unused-vars': ['warn', {
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^_',
        caughtErrorsIgnorePattern: '^_',
      }],
      // Existing route-view filenames are established public component names.
      'vue/multi-word-component-names': 'off',
    },
  },
)

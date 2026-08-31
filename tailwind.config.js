/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["seatbot/web/templates/**/*.html"],
  safelist: [
    'cell-card',
    'chip', 'chip-accent', 'chip-success', 'chip-progress',
    'chip-warn', 'chip-danger', 'chip-muted', 'chip-target',
  ],
  darkMode: ['class', '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        app: {
          DEFAULT: 'rgb(var(--bg-app-rgb) / <alpha-value>)',
          elevated: 'rgb(var(--bg-elevated-rgb) / <alpha-value>)',
          surface: 'rgb(var(--bg-surface-rgb) / <alpha-value>)',
          muted: 'rgb(var(--bg-muted-rgb) / <alpha-value>)',
          sidebar: 'rgb(var(--bg-sidebar-rgb) / <alpha-value>)',
          'table-header': 'rgb(var(--bg-table-header-rgb) / <alpha-value>)',
        },
        /* 描边 token 的基础透明度在 token 层控制（--border-*-a），类级 alpha 修饰符与其相乘；
           深色描边是半透明白（10%/17%），不能用 <alpha-value> 直译成不透明色 */
        edge: {
          subtle: 'rgb(var(--border-subtle-rgb) / calc(<alpha-value> * var(--border-subtle-a)))',
          strong: 'rgb(var(--border-strong-rgb) / calc(<alpha-value> * var(--border-strong-a)))',
        },
        ink: {
          DEFAULT: 'rgb(var(--text-primary-rgb) / <alpha-value>)',
          secondary: 'rgb(var(--text-secondary-rgb) / <alpha-value>)',
          muted: 'rgb(var(--text-muted-rgb) / <alpha-value>)',
          inverse: 'rgb(var(--text-inverse-rgb) / <alpha-value>)',
        },
        accent: {
          DEFAULT: 'rgb(var(--accent-rgb) / <alpha-value>)',
          hover: 'rgb(var(--accent-hover-rgb) / <alpha-value>)',
          soft: 'var(--accent-soft)',
        },
        progress: { DEFAULT: 'rgb(var(--progress-rgb) / <alpha-value>)', soft: 'var(--progress-soft)' },
        target:  { DEFAULT: 'rgb(var(--target-rgb) / <alpha-value>)',  soft: 'var(--target-soft)' },
        success: { DEFAULT: 'rgb(var(--success-rgb) / <alpha-value>)', soft: 'var(--success-soft)' },
        warn:    { DEFAULT: 'rgb(var(--warn-rgb) / <alpha-value>)',    soft: 'var(--warn-soft)' },
        danger:  { DEFAULT: 'rgb(var(--danger-rgb) / <alpha-value>)',  soft: 'var(--danger-soft)' },
        info:    { DEFAULT: 'rgb(var(--info-rgb) / <alpha-value>)',    soft: 'var(--info-soft)' },
      },
      fontFamily: {
        sans: ['"Public Sans"','-apple-system','BlinkMacSystemFont','"Segoe UI"','"Microsoft YaHei"','"PingFang SC"','system-ui','sans-serif'],
        mono: ['"JetBrains Mono"','"SF Mono"','Consolas','monospace'],
      },
      fontSize: {
        xs: ['12px','1.4'],
        sm: ['13px','1.45'],
        base: ['14px','1.55'],
        md: ['15px','1.55'],
        lg: ['18px','1.4'],
        xl: ['22px','1.3'],
        '2xl': ['28px','1.2'],
      },
      maxWidth: { content: '1440px' },
      boxShadow: {
        sm: '0 1px 2px rgba(0,0,0,0.05)',
        md: '0 4px 12px rgba(0,0,0,0.08)',
        lg: '0 8px 24px rgba(0,0,0,0.12)',
      },
    },
  },
  plugins: [],
};

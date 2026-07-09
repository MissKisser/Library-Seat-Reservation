/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["seatbot/web/templates/**/*.html"],
  darkMode: ['class', '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        app: {
          DEFAULT: 'var(--bg-app)',
          elevated: 'var(--bg-elevated)',
          surface: 'var(--bg-surface)',
          muted: 'var(--bg-muted)',
        },
        edge: {
          subtle: 'var(--border-subtle)',
          strong: 'var(--border-strong)',
        },
        ink: {
          DEFAULT: 'var(--text-primary)',
          secondary: 'var(--text-secondary)',
          muted: 'var(--text-muted)',
          inverse: 'var(--text-inverse)',
        },
        accent: {
          DEFAULT: 'var(--accent)',
          hover: 'var(--accent-hover)',
          soft: 'var(--accent-soft)',
        },
        success: { DEFAULT: 'var(--success)', soft: 'var(--success-soft)' },
        warn:    { DEFAULT: 'var(--warn)',    soft: 'var(--warn-soft)' },
        danger:  { DEFAULT: 'var(--danger)',  soft: 'var(--danger-soft)' },
        info:    { DEFAULT: 'var(--info)',    soft: 'var(--info-soft)' },
      },
      fontFamily: {
        sans: ['-apple-system','BlinkMacSystemFont','"Segoe UI"','"Microsoft YaHei"','"PingFang SC"','system-ui','sans-serif'],
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

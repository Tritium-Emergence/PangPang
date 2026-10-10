/** @type {import('tailwindcss').Config} */
export default {
  content: ['./web-src/**/*.{ts,tsx,html}'],
  theme: {
    extend: {
      colors: {
        // Product surface palette (consumed via CSS vars so a later theme pass is cheap).
        ink: 'var(--ink)',
        muted: 'var(--muted)',
        line: 'var(--line)',
        surface: 'var(--surface)',
        raised: 'var(--raised)',
        brand: 'var(--brand)',
        warn: 'var(--warn)',
        good: 'var(--good)',
      },
    },
  },
  plugins: [],
};

import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';
import {resolve} from 'node:path';

// Single-file product bundle for P2-1. Python serves exactly four static routes
// (/, /app.js, /app.css, /setup.js) with CSP `script-src 'self'; style-src 'self';
// img-src 'self' data:` — so the whole React app must emit as ONE external
// `pangpang/web/app.js` + ONE `pangpang/web/app.css` (NOT inlined into HTML; the CSP
// forbids inline <script>/<style>). Images/icons inline as data: URIs. No hashed
// /assets/* (the server has no route for them). The hand-written index.html is kept
// as-is (it already loads /app.js + /app.css); this build only overwrites those two.
export default defineConfig({
  plugins: [react()],
  root: resolve(__dirname, 'web-src'),
  build: {
    // Builds emit to a gitignored web-dist/ scratch dir. The committed production shell
    // lives at pangpang/web/{app.js,app.css}; the web gate
    // rebuilds here and byte-compares against the committed shell to prove they match.
    outDir: resolve(__dirname, process.env.P21_OUTDIR || 'web-dist'),
    emptyOutDir: false, // keep index.html / setup.js — only overwrite app.js/app.css
    cssCodeSplit: false, // one stylesheet
    sourcemap: false,
    assetsInlineLimit: 100 * 1024 * 1024, // inline every asset as data: (CSP img-src allows data:)
    modulePreload: false,
    rollupOptions: {
      input: resolve(__dirname, 'web-src/main.tsx'),
      output: {
        inlineDynamicImports: true, // single JS chunk
        entryFileNames: 'app.js',
        assetFileNames: (info) => (info.name && info.name.endsWith('.css') ? 'app.css': 'assets/[name][extname]'),
      },
    },
  },
});

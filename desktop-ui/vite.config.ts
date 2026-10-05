import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

// `npm run dev` proxies to a running `agent8s-desktop --no-window --port 8765 --token dev`.
const backend = process.env.AGENT8S_BACKEND ?? 'http://127.0.0.1:8765';

export default defineConfig({
  // Relative URLs: the same bundle is served at / by the desktop app and at /agent8s/ by the relay.
  base: './',
  plugins: [svelte()],
  build: {
    outDir: '../src/agent8s/desktop/web',
    emptyOutDir: true,
    target: 'safari15',
    sourcemap: false,
  },
  server: {
    proxy: {
      '/api': { target: backend, changeOrigin: true },
      '/ws': { target: backend, changeOrigin: true, ws: true },
    },
  },
});

import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [sveltekit()],
  // FastAPI serves the SPA on the same origin as /api/*, so no proxy needed
  // in prod. For local SvelteKit dev (`npm run dev` on :5173), proxy to htv.
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:3000'
    }
  }
});

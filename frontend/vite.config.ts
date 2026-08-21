import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies /api to the ZBS API (uvicorn on 8080), so `npm run dev`
// behaves exactly like the containerized nginx setup.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8080",
    },
  },
  preview: {
    proxy: {
      "/api": "http://127.0.0.1:8080",
    },
  },
});

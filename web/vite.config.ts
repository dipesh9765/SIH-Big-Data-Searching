import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// In development the UI runs on :5173 and forwards API calls to the service on :8000.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/v1": "http://127.0.0.1:8000" } },
  build: { outDir: "dist", sourcemap: false },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});

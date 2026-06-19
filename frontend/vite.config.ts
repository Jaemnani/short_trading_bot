import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Responsive PWA dashboard. API base is configurable via VITE_API_BASE.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
});

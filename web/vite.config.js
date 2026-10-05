import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` serves the site on http://localhost:5173 and passes /api
// calls to a server started with `python -m phonebench serve`.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});

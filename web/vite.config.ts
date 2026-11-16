/** 静态前端同源部署；依赖按实际页面加载，PDF worker 由 Vite 生成受控资源。 */
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig({
  plugins: [react()],
  build: {
    sourcemap: false,
    target: "es2022",
    chunkSizeWarningLimit: 700,
    license: { fileName: "third-party-licenses.md" },
  },
  server: { host: "127.0.0.1" },
});

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  // 相对基径：构建产物中的 JS/CSS 等资源引用相对于页面地址解析，
  // 支持把面板反向代理到任意子路径（如 /AGY2API/）下访问
  base: "./",
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      "/auth": {
        target: "http://localhost:7861",
        changeOrigin: true,
      },
      "/creds": {
        target: "http://localhost:7861",
        changeOrigin: true,
      },
      "/config": {
        target: "http://localhost:7861",
        changeOrigin: true,
      },
      "/usage-logs": {
        target: "http://localhost:7861",
        changeOrigin: true,
      },
      "/logs": {
        target: "http://localhost:7861",
        changeOrigin: true,
        ws: true,
      },
      "/version": {
        target: "http://localhost:7861",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "../dist",
    emptyOutDir: true,
  },
});

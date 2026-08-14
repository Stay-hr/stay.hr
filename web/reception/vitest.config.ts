import path from "node:path";
import { defineConfig } from "vitest/config";

const alias = { "@": path.resolve(__dirname, ".") };

export default defineConfig({
  esbuild: {
    jsx: "automatic",
  },
  resolve: {
    alias,
  },
  test: {
    projects: [
      {
        resolve: { alias },
        test: {
          name: "unit",
          environment: "node",
          include: ["lib/**/*.test.ts"],
        },
      },
      {
        esbuild: { jsx: "automatic" },
        resolve: { alias },
        test: {
          name: "component",
          environment: "jsdom",
          include: ["app/**/*.test.tsx"],
        },
      },
    ],
  },
});

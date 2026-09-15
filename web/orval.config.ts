import { defineConfig } from "orval";

export default defineConfig({
  core: {
    input: { target: "./openapi.json" },
    output: {
      target: "./src/generated/core.ts",
      schemas: "./src/generated/models",
      client: "fetch",
      mode: "single",
      baseUrl: "/api/core",
      clean: true,
    },
  },
});

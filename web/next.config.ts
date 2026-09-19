import type { NextConfig } from "next";
import { createMDX } from "fumadocs-mdx/next";

const nextConfig: NextConfig = {
  allowedDevOrigins: ["priv.augment.ws"],
  reactCompiler: true,
};

const withMDX = createMDX();

export default withMDX(nextConfig);

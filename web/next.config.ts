import type { NextConfig } from "next";
import { createMDX } from "fumadocs-mdx/next";

const nextConfig: NextConfig = {
  allowedDevOrigins: ["priv.augment.ws"],
  // Keep the development tools badge away from the mobile navigation trigger.
  devIndicators: { position: "bottom-right" },
  reactCompiler: true,
};

const withMDX = createMDX();

export default withMDX(nextConfig);

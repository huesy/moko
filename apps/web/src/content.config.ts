import { defineCollection } from "astro:content";
import { docsLoader } from "@astrojs/starlight/loaders";
import { docsSchema } from "@astrojs/starlight/schema";

// Starlight's docs collection. Files live under src/content/docs/docs/** so
// every page is served under /docs/* and the marketing landing keeps owning /.
export const collections = {
  docs: defineCollection({ loader: docsLoader(), schema: docsSchema() }),
};

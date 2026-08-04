import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import { copyFileSync, existsSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));

// Serve the one-line installers from the site root (mokomoko.ai/install.sh and
// /install.ps1). The canonical copies live at the repo root; this copies them
// into public/ on every dev/build so there's a single source of truth (the
// public copies are gitignored). Runs via a config hook, so it fires however
// the host invokes the build, not just through our package.json script.
function installers() {
  return {
    name: 'moko-installers',
    hooks: {
      'astro:config:setup'({ logger }) {
        const root = resolve(here, '../../');
        const pub = resolve(here, 'public');
        if (!existsSync(pub)) mkdirSync(pub, { recursive: true });
        for (const file of ['install.sh', 'install.ps1']) {
          const src = resolve(root, file);
          if (existsSync(src)) copyFileSync(src, resolve(pub, file));
          else logger.warn(`installer missing, not served: ${src}`);
        }
      },
    },
  };
}

// Drives canonical URLs + the sitemap; the install commands on the page
// reference this host too.
export default defineConfig({
  site: 'https://mokomoko.ai',
  // Bind the dev/preview server to all interfaces so it's reachable from
  // other devices on the LAN (phone, another laptop) — not just localhost.
  server: { host: true, port: 4321 },
  integrations: [
    installers(),
    // Docs live under /docs (content nested in src/content/docs/docs/**), so
    // the bespoke marketing landing at src/pages/index.astro keeps owning /.
    starlight({
      title: 'Moko',
      description:
        "Moko — a persistent, local-first personal AI with a memory graph, an inner life, and genuine curiosity. Documentation.",
      tagline: 'A warm, curious creature that lives on your machine.',
      customCss: ['./src/styles/docs.css'],
      // GTM: Head injects the bootstrap script first; SkipLink wraps the
      // noscript iframe so it sits right after <body> (SkipLink is Starlight's
      // first body child). Both are MPA full-page loads — no SPA history hooks.
      components: {
        Head: './src/components/starlight/Head.astro',
        SkipLink: './src/components/starlight/SkipLink.astro',
      },
      social: [{ icon: 'github', label: 'GitHub', href: 'https://github.com/huesy/moko' }],
      editLink: { baseUrl: 'https://github.com/huesy/moko/edit/main/apps/web/' },
      sidebar: [
        {
          label: 'Start here',
          items: [
            { label: 'What Moko is', slug: 'docs' },
            { label: 'Install & setup', slug: 'docs/install' },
            { label: 'Daily life with Moko', slug: 'docs/daily-life' },
          ],
        },
        {
          label: 'How Moko thinks',
          items: [
            { label: 'The memory graph', slug: 'docs/concepts/memory-graph' },
            { label: 'Thoughts & the honesty boundary', slug: 'docs/concepts/inner-life' },
            { label: 'Curiosity', slug: 'docs/concepts/curiosity' },
            { label: 'Proactivity & the governors', slug: 'docs/concepts/proactivity' },
            { label: 'Dreaming', slug: 'docs/concepts/dreaming' },
            { label: 'Senses & its world', slug: 'docs/concepts/senses' },
            { label: 'Time awareness', slug: 'docs/concepts/time' },
          ],
        },
        {
          label: 'Safety',
          items: [{ label: 'The safety model', slug: 'docs/safety' }],
        },
        {
          label: 'Reference',
          items: [
            { label: 'CLI', slug: 'docs/reference/cli' },
            { label: 'The monitor', slug: 'docs/reference/monitor' },
          ],
        },
      ],
    }),
  ],
});

/** Google Tag Manager container id — marketing site + Starlight docs. */
export const GTM_ID = "GTM-53R3QNWZ";

/** Inline bootstrap script (paste into <head> as high as possible). */
export const gtmHeadScript = `(function(w,d,s,l,i){w[l]=w[l]||[];w[l].push({'gtm.start':
new Date().getTime(),event:'gtm.js'});var f=d.getElementsByTagName(s)[0],
j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';j.async=true;j.src=
'https://www.googletagmanager.com/gtm.js?id='+i+dl;f.parentNode.insertBefore(j,f);
})(window,document,'script','dataLayer','${GTM_ID}');`;

/**
 * Client helper: push a custom event into GTM's dataLayer.
 * Keep the function tiny and inline-friendly (no imports at call sites in Astro
 * <script> islands that don't go through the module graph).
 *
 * Events we fire today:
 *   moko_install_copy — user copied the one-line installer (platform=macos|linux|windows)
 */
export function trackEvent(event: string, params: Record<string, string | number | boolean> = {}): void {
  const w = window as Window & { dataLayer?: Record<string, unknown>[] };
  w.dataLayer = w.dataLayer || [];
  w.dataLayer.push({ event, ...params });
}

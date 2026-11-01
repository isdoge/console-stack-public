/// <reference types="node" />

import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const frontendRoot = process.cwd();
const html = readFileSync(resolve(frontendRoot, 'index.html'), 'utf8');
const manifest = JSON.parse(
  readFileSync(resolve(frontendRoot, 'public/manifest.webmanifest'), 'utf8'),
);

describe('PWA metadata', () => {
  it('declares the manifest and iOS home-screen metadata', () => {
    expect(html).toContain('<link rel="manifest" href="/manifest.webmanifest" />');
    expect(html).toContain(
      '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png" />',
    );
    expect(html).toContain(
      '<meta name="apple-mobile-web-app-capable" content="yes" />',
    );
    expect(html).toContain(
      '<meta name="apple-mobile-web-app-status-bar-style" content="default" />',
    );
  });

  it('does not load Google Fonts or other remote font services at runtime', () => {
    expect(html).not.toMatch(/fonts\.googleapis\.com/);
    expect(html).not.toMatch(/fonts\.gstatic\.com/);
    expect(html).not.toMatch(/use\.typekit\.net/);
  });

  it('pins the standalone manifest contract and existing icon files', () => {
    expect(manifest).toEqual({
      name: '自托管运维总控台',
      short_name: '运维总控台',
      icons: [
        {
          src: '/pwa-192x192.png',
          sizes: '192x192',
          type: 'image/png',
        },
        {
          src: '/pwa-512x512.png',
          sizes: '512x512',
          type: 'image/png',
        },
      ],
      theme_color: '#0ea5e9',
      background_color: '#f1f5f9',
      start_url: '/',
      display: 'standalone',
    });

    for (const icon of [...manifest.icons, { src: '/apple-touch-icon.png' }]) {
      expect(
        existsSync(resolve(frontendRoot, `public${icon.src}`)),
        `${icon.src} should exist`,
      ).toBe(true);
    }
  });
});

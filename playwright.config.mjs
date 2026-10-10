import {defineConfig} from '@playwright/test';
// Two fixtures (tests/browser_fixture.py <scenario> <port>): `onboarding` keeps the
// original first-run flow verbatim; `studio` is a configured, pre-seeded coordinator
// for the studio behavior contracts. UTF-8 is forced so the fixture subprocess never
// mis-decodes Chinese payloads on a cp936 host.
const env = {PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8'};
const base = {screenshot: 'only-on-failure', trace: 'retain-on-failure'};
export default defineConfig({
  testDir: './tests/browser', workers: 1, timeout: 60000,
  reporter: [['list'], ['html', {open: 'never'}]],
  webServer: [
    {command: 'python tests/browser_fixture.py onboarding 8799', url: 'http://127.0.0.1:8799/', timeout: 60000, reuseExistingServer: false, env},
    {command: 'python tests/browser_fixture.py studio 8798', url: 'http://127.0.0.1:8798/', timeout: 60000, reuseExistingServer: false, env},
    {command: 'python tests/browser_fixture.py correction 8797', url: 'http://127.0.0.1:8797/', timeout: 60000, reuseExistingServer: false, env},
  ],
  projects: [
    {name: 'onboarding', testMatch: /onboarding\.spec\.mjs/, use: {...base, baseURL: 'http://127.0.0.1:8799'}},
    {name: 'studio', testMatch: /studio-.*\.spec\.mjs/, use: {...base, baseURL: 'http://127.0.0.1:8798'}},
    {name: 'correction', testMatch: /correction-.*\.spec\.mjs/, use: {...base, baseURL: 'http://127.0.0.1:8797'}},
...(process.env.P21_WEBKIT ? [{name: 'webkit-mobile', testMatch: /studio-(?:mobile|parity-(?:mobile|boundaries))\.spec\.mjs/, use: {...base, browserName: 'webkit', isMobile: true, hasTouch: true, baseURL: 'http://127.0.0.1:8798'}}]: []),
  ],
});

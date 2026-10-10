import i18n from 'i18next';
import {initReactI18next} from 'react-i18next';
import zhCN from './zh-CN';
import enUS from './en-US';

// Presentation only. Locale selects which label string renders; it never selects
// permissions, tools, or state logic (those use raw server codes). UI preference
// lives in localStorage — this is a single-user local product; language belongs to
// the device, not to any Bot or task.
export type Locale = 'zh-CN' | 'en-US';
const LOCALES: Locale[] = ['zh-CN', 'en-US'];
const KEY = 'pangpang.uiLocale.v1';

const whitelist = (v: string | null | undefined): Locale | null =>
  LOCALES.includes(v as Locale) ? (v as Locale): null;

function fromBrowser(): Locale {
  for (const l of (typeof navigator !== 'undefined' ? navigator.languages || [navigator.language]: [])) {
    if (l?.startsWith('zh')) return 'zh-CN';
    if (l?.startsWith('en')) return 'en-US';
  }
  return 'en-US';
}

export function readLocale(): Locale {
  try { return whitelist(localStorage.getItem(KEY)) ?? fromBrowser(); }
  catch { return fromBrowser(); }
}

export function setLocale(l: Locale) {
  const next = whitelist(l) ?? 'en-US';
  try { localStorage.setItem(KEY, next); } catch { /* storage may be unavailable */ }
  if (typeof document !== 'undefined') document.documentElement.lang = next;
  return i18n.changeLanguage(next);
}

const initial = readLocale();
i18n.use(initReactI18next).init({
  resources: {'zh-CN': {translation: zhCN}, 'en-US': {translation: enUS}},
  lng: initial,
  fallbackLng: 'en-US',
  interpolation: {escapeValue: false},
});
if (typeof document !== 'undefined') document.documentElement.lang = initial;

export default i18n;

import {shellFrameCss, viewportAction} from './pure';

// Pin #shell to the visible viewport while the keyboard is open, and drop a stuck
// pan once the keyboard is gone. An empty adopted sheet leaves the CSS lock in charge.
// style-src 'self' blocks element.style, so the rule lives in a constructed sheet.
export function bindShellViewport(win: Window = window): () => void {
  const vv = win.visualViewport;
  const StyleSheet = (win as Window & {CSSStyleSheet?: typeof CSSStyleSheet}).CSSStyleSheet;
  if (!vv || typeof StyleSheet !== 'function') return () => {};
  let sheet: CSSStyleSheet | null = null;
  let current = '';
  let resetFor = -1;
  const write = (css: string) => {
    if (css === current) return;
    current = css;
    try {
      if (!sheet) {
        const next = new StyleSheet();
        win.document.adoptedStyleSheets = [...win.document.adoptedStyleSheets, next];
        sheet = next;
      }
      sheet.replaceSync(css);
    } catch {
      current = '';
    }
  };
  const apply = () => {
    const action = viewportAction(
      {height: vv.height, offsetTop: vv.offsetTop},
      {innerHeight: win.innerHeight},
    );
    if (action.type === 'reset') {
      const key = Math.round(vv.offsetTop);
      if (resetFor !== key) {
        resetFor = key;
        win.scrollTo(0, 0);
      }
      write('');
      return;
    }
    resetFor = -1;
    write(action.type === 'pin' ? shellFrameCss(action) : '');
  };
  apply();
  vv.addEventListener('resize', apply);
  vv.addEventListener('scroll', apply);
  win.addEventListener('orientationchange', apply);
  return () => {
    vv.removeEventListener('resize', apply);
    vv.removeEventListener('scroll', apply);
    win.removeEventListener('orientationchange', apply);
    write('');
  };
}

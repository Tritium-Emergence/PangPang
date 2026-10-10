import {useLayoutEffect, useRef, type ReactNode} from 'react';
import {createPortal} from 'react-dom';

// WebKit/Safari does not focus a <button> when it is pressed (a platform rule, not a
// bug), so a modal opened from a button press mounts with document.activeElement
// still on <body> and has no opener left to restore — Escape would strand focus at
// the top of the document. One capture-phase pointerdown listener records the control
// that was actually pressed: pointerdown fires before click on every engine and does
// not depend on the engine focusing its target, and capture means app code cannot
// hide it by stopping propagation. The reference is kept until the NEXT pointerdown
// on purpose, so a dialog opened asynchronously after the click (HostsView awaits the
// server for its pair code) still has its opener; a press on anything unfocusable
// clears it rather than leaving a stale control behind.
let pressed: HTMLElement | null = null;
if (typeof document !== 'undefined') document.addEventListener('pointerdown', (e) => {
  pressed = e.target instanceof Element ? e.target.closest<HTMLElement>('button, a, [tabindex]'): null;
}, true);

// Native top layer supplies focus trapping, inert background and Escape handling.
export function Modal({id, title, onClose, children, className = ''}: {id: string; title: string; onClose: () => void; children: ReactNode; className?: string}) {
  const ref = useRef<HTMLDialogElement>(null);
  useLayoutEffect(() => {
    const d = ref.current!, active = document.activeElement;
    // A genuinely focused opener wins (keyboard activation, and engines that focus on
    // press); <body>/<html> mean nothing is focused, so fall back to the pressed control.
    const opener = active instanceof HTMLElement && active !== document.body && active !== document.documentElement ? active: pressed;
    d.showModal();
    // Close before React removes the dialog; otherwise native focus restoration
    // can lose its opener when passive cleanup runs after DOM detachment.
    return () => {
      if (d.open) d.close();
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus({preventScroll: true});
    };
  }, []);
  return createPortal(<dialog ref={ref} id={id} aria-label={title} className={`modal rounded-2xl border border-line bg-raised p-5 shadow-xl ${className}`} onCancel={(e) => { e.preventDefault(); onClose(); }} onClick={(e) => { if (e.target === e.currentTarget) { const r = e.currentTarget.getBoundingClientRect(); if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) onClose(); } }}>
    {children}
  </dialog>, document.body);
}

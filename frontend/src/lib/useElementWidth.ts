import { useEffect, useRef, useState } from 'react';

/**
 * The rendered width of an element, kept current with a ResizeObserver.
 *
 * SVG plots draw in real pixels at this width instead of scaling a fixed viewBox,
 * so their labels stay the same readable size on a phone and on a desktop.
 */
export function useElementWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setWidth(el.getBoundingClientRect().width);
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) setWidth(entry.contentRect.width);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

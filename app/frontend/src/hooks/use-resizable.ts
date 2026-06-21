import { useEffect, useRef, useState } from 'react';

interface UseResizableOptions {
  minWidth?: number;
  maxWidth?: number;
  defaultWidth?: number;
  minHeight?: number;
  maxHeight?: number;
  defaultHeight?: number;
  side?: 'left' | 'right' | 'bottom';
}

export function useResizable({
  minWidth = 200,
  maxWidth = 500,
  defaultWidth = 250,
  minHeight = 200,
  maxHeight = 600,
  defaultHeight = 300,
  side = 'left'
}: UseResizableOptions = {}) {
  const [width, setWidth] = useState(defaultWidth);
  const [height, setHeight] = useState(defaultHeight);
  const [isDragging, setIsDragging] = useState(false);
  const elementRef = useRef<HTMLDivElement>(null);

  // Begin a resize: just flip drag state. The document listeners are attached by the effect
  // below (keyed on isDragging) so their lifecycle is always paired with the drag — no stale
  // closures and no leak if the component unmounts mid-drag.
  const startResize = (e: React.MouseEvent) => {
    e.preventDefault();
    setIsDragging(true);
  };

  // Attach mousemove/mouseup ONLY while dragging. The handlers are created inside the effect,
  // so they close over the CURRENT minWidth/maxWidth/minHeight/maxHeight/side (deps below) —
  // a mid-drag bounds/side change re-attaches fresh handlers. The cleanup removes exactly the
  // handlers that were attached (including on unmount during a drag), so nothing leaks.
  useEffect(() => {
    if (!isDragging) return;

    const handleMouseMove = (e: MouseEvent) => {
      const elementRect = elementRef.current?.getBoundingClientRect();
      if (!elementRect) return;

      if (side === 'bottom') {
        // For bottom panel: dragging up decreases height
        const newHeight = elementRect.bottom - e.clientY;
        setHeight(Math.max(minHeight, Math.min(maxHeight, newHeight)));
      } else {
        // For horizontal resizing (left/right sidebars)
        let newWidth;
        if (side === 'left') {
          // For left sidebar: dragging right increases width
          newWidth = e.clientX - elementRect.left;
        } else {
          // For right sidebar: dragging left decreases width
          newWidth = elementRect.right - e.clientX;
        }
        setWidth(Math.max(minWidth, Math.min(maxWidth, newWidth)));
      }
    };

    const stopResize = () => setIsDragging(false);

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', stopResize);

    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', stopResize);
    };
  }, [isDragging, minWidth, maxWidth, minHeight, maxHeight, side]);

  return {
    width,
    height,
    isDragging,
    elementRef,
    startResize
  };
}

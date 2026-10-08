/* Prepare the browser-like environment used by React component tests.
   The assertion helpers make page elements easy to check, while a scroll stub
   keeps navigation tests stable because jsdom has no real viewport scrolling. */
import '@testing-library/jest-dom/vitest';

Object.defineProperty(window, 'scrollTo', { value: () => {}, writable: true });

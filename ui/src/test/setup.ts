// Add DOM assertions and a scroll stub for the component tests.
import '@testing-library/jest-dom/vitest';

Object.defineProperty(window, 'scrollTo', { value: () => {}, writable: true });

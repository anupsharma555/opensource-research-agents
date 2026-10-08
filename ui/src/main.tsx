/* Put the sample research-review app on the page.
   Vite uses this entry point during development, and Tauri bundles its built
   output for the Mac window. The app component loads the saved sample snapshots. */
import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);

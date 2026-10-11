/* Format a recorded time interval without extending it to the viewer's clock. */
export function savedDuration(start: string | null | undefined, finish: string | null | undefined): string | null {
  if (!start || !finish) return null;
  const elapsed = Date.parse(finish) - Date.parse(start);
  if (!Number.isFinite(elapsed) || elapsed < 0) return null;
  const seconds = Math.round(elapsed / 1000);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  return [hours ? `${hours} hr` : '', hours || minutes ? `${minutes} min` : '', `${seconds % 60} sec`].filter(Boolean).join(' ');
}

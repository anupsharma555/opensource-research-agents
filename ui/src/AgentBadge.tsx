/* Identify an agent from a saved name and ID wherever recorded work is shown.
   The initial and stable color help scanning, while visible text carries identity
   without depending on color or implying an agent when the name is unavailable. */
export function AgentBadge({ name, agentId }: { name?: string | null; agentId?: string | null }) {
  const savedName = name?.trim();
  const label = savedName ? `Agent ${savedName}` : agentId ? `Agent identity unavailable (${agentId})` : 'Agent identity unavailable';
  const initial = savedName ? savedName[0].toLocaleUpperCase() : '?';
  const tone = agentId ? [...agentId].reduce((hash, character) => (hash * 31 + character.charCodeAt(0)) % 4, 0) : 4;
  return <span className={`agent-badge agent-badge-tone-${tone}`}><span className="agent-badge-initial" aria-hidden="true">{initial}</span><span>{label}</span></span>;
}

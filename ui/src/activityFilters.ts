/* Group related Activity controls under one reducer.
   Changing research context resets the filter set atomically; authoritative
   attempts and events remain in the backend snapshot. */
import { useCallback, useReducer } from 'react';
import type { ActivityItem } from './ActivityRecordDetails';

type Filters = { order: 'task' | 'time'; filter: 'all' | ActivityItem['category']; agentId: string; search: string; attentionOnly: boolean };
type Action = { type: 'reset' } | { type: 'change'; values: Partial<Filters> };
const initial: Filters = { order: 'time', filter: 'agent', agentId: 'all', search: '', attentionOnly: false };
export function activityFilterReducer(state: Filters, action: Action): Filters {
  return action.type === 'reset' ? { ...initial, order: state.order, filter: state.filter } : { ...state, ...action.values };
}
export function useActivityFilters() {
  const [filters, dispatch] = useReducer(activityFilterReducer, initial);
  return {
    ...filters,
    reset: useCallback(() => dispatch({ type: 'reset' }), []),
    setOrder: useCallback((order: Filters['order']) => dispatch({ type: 'change', values: { order } }), []),
    setFilter: useCallback((filter: Filters['filter']) => dispatch({ type: 'change', values: { filter } }), []),
    setAgentId: useCallback((agentId: string) => dispatch({ type: 'change', values: { agentId } }), []),
    setSearch: useCallback((search: string) => dispatch({ type: 'change', values: { search } }), []),
    setAttentionOnly: useCallback((attentionOnly: boolean) => dispatch({ type: 'change', values: { attentionOnly } }), []),
  };
}

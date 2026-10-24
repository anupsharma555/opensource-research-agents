/* Generated from contracts/generated/search_evidence_library.schema.json. Do not edit by hand. */
export type Cursor = number;
export type Entryid = string | null;
export type Limit = number;
export type Query = string | null;

export interface SearchEvidenceLibraryInput {
  cursor?: Cursor;
  entryId?: Entryid;
  limit?: Limit;
  query?: Query;
}

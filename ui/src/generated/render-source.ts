/* Generated from contracts/generated/render_source.schema.json. Do not edit by hand. */
export type Id = string;
export type Kind = "public_document_fetch";
export type Version = number;
/**
 * What JavaScript-dependent content is missing from ordinary extraction.
 */
export type Reason = string;

export interface RenderSourceInput {
  ordinaryDocumentRef: PublicDocumentRef;
  reason: Reason;
}
/**
 * Exact kind/id/version of this task's saved ordinary public_document_fetch.
 */
export interface PublicDocumentRef {
  id: Id;
  kind: Kind;
  version: Version;
}

/* Generated from contracts/generated/inspect_figure.schema.json. Do not edit by hand. */
export type Id = string;
export type Kind = "public_document_fetch";
export type Version = number;
export type Pagenumber = number;
/**
 * Specific visual question about this page or figure. Returns an unverified model interpretation, not a checked source passage.
 */
export type Question = string;

export interface InspectFigureInput {
  documentRef: PublicDocumentRef;
  pageNumber: Pagenumber;
  question: Question;
}
export interface PublicDocumentRef {
  id: Id;
  kind: Kind;
  version: Version;
}

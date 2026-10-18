/* Generated from contracts/generated/read_document_ocr.schema.json. Do not edit by hand. */
export type Id = string;
export type Kind = "public_document_fetch";
export type Version = number;
/**
 * Auto: whole-page layout, or one block for a region. Block: one text block. Sparse: separated labels, without reliable reading order. OCR is unverified transcription, not figure interpretation.
 */
export type Layout = "auto" | "block" | "sparse";
/**
 * Maximum OCR characters returned; use next_request to read more.
 */
export type Limit = number;
export type Offset = number;
/**
 * One selected PDF page, or 1 for a saved PNG/JPEG. Local OCR; no model API call.
 */
export type Pagenumber = number;
export type Height = number;
export type Width = number;
export type X = number;
export type Y = number;

export interface ReadDocumentOCRInput {
  documentRef: PublicDocumentRef;
  layout?: Layout;
  limit?: Limit;
  offset?: Offset;
  pageNumber: Pagenumber;
  /**
   * Optional rectangle x/y/width/height in 0–1 fractions from top-left of the rendered PDF page or EXIF-oriented image. Use known coordinates to isolate text; omitted reads the page.
   */
  region?: OCRRegion | null;
}
export interface PublicDocumentRef {
  id: Id;
  kind: Kind;
  version: Version;
}
/**
 * Selected rectangle on the rendered PDF page or EXIF-oriented image.
 */
export interface OCRRegion {
  height: Height;
  width: Width;
  x: X;
  y: Y;
}

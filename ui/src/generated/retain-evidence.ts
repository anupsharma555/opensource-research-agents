/* Generated from contracts/generated/retain_evidence.schema.json. Do not edit by hand. */
/**
 * @maxItems 10
 */
export type Findingrefs =
  | []
  | [EvidenceFindingRef]
  | [EvidenceFindingRef, EvidenceFindingRef]
  | [EvidenceFindingRef, EvidenceFindingRef, EvidenceFindingRef]
  | [EvidenceFindingRef, EvidenceFindingRef, EvidenceFindingRef, EvidenceFindingRef]
  | [EvidenceFindingRef, EvidenceFindingRef, EvidenceFindingRef, EvidenceFindingRef, EvidenceFindingRef]
  | [
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef
    ]
  | [
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef
    ]
  | [
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef
    ]
  | [
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef
    ]
  | [
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef,
      EvidenceFindingRef
    ];
export type Id = string;
export type Kind = "report";
export type Version = number;
export type Relevancenote = string;
/**
 * @minItems 1
 * @maxItems 10
 */
export type Sourcerefs =
  | [EvidenceSourceRef]
  | [EvidenceSourceRef, EvidenceSourceRef]
  | [EvidenceSourceRef, EvidenceSourceRef, EvidenceSourceRef]
  | [EvidenceSourceRef, EvidenceSourceRef, EvidenceSourceRef, EvidenceSourceRef]
  | [EvidenceSourceRef, EvidenceSourceRef, EvidenceSourceRef, EvidenceSourceRef, EvidenceSourceRef]
  | [EvidenceSourceRef, EvidenceSourceRef, EvidenceSourceRef, EvidenceSourceRef, EvidenceSourceRef, EvidenceSourceRef]
  | [
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef
    ]
  | [
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef
    ]
  | [
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef
    ]
  | [
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef,
      EvidenceSourceRef
    ];
export type Id1 = string;
export type Kind1 = "source_copy";
export type Version1 = number;

export interface RetainEvidenceInput {
  findingRefs?: Findingrefs;
  relevanceNote: Relevancenote;
  sourceRefs: Sourcerefs;
}
export interface EvidenceFindingRef {
  id: Id;
  kind: Kind;
  version: Version;
}
export interface EvidenceSourceRef {
  id: Id1;
  kind: Kind1;
  version: Version1;
}

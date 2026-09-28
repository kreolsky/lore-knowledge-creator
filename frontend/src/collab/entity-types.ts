/**
 * Single point for entity-kind vocabulary on the multiplexed collab channel.
 *
 * The 'doc' string literal exists HERE and nowhere else: consumers import the
 * `DOC` runtime constant and the `EntityType` type instead of re-spelling the
 * literal (the provider stores the type per entity and rejoins from the stored
 * value — see yjs-provider EntityYjsState.entityType).
 */
export const ENTITY_TYPES = ['doc'] as const;

export type EntityType = (typeof ENTITY_TYPES)[number];

/** Runtime constant for the (currently only) entity kind. */
export const DOC: EntityType = ENTITY_TYPES[0];

/**
 * Compartment NoteConnector reconfigures with an updateListener on mount (and
 * clears on unmount) — the leak-free attach/remove seam for recomputing the
 * connector's geometry on editor updates. Kept inert ([]) in every editor whose
 * connector is not connected. Why a Compartment: it is the only leak-free way
 * to attach then REMOVE an updateListener from a view the connector does not
 * own (appendConfig cannot be surgically reversed). Mirrors find-matches.ts
 * searchPanelListenerCompartment.
 */
import { Compartment } from '@codemirror/state';

export const noteConnectorListenerCompartment = new Compartment();

/** Inert inclusion for base editor configs (cmSetup). */
export const noteConnectorRefresh = noteConnectorListenerCompartment.of([]);

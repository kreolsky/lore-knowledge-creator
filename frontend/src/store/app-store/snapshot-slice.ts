/** Snapshot slice — checkpoint preview pointer + the create-snapshot modal's pending payload.

# ARCH (debt-paydown W6): second app-store slice. Read by HistoryPanel (preview selection),
# SnapshotModal (pending content) and DocumentTree (the "viewing a snapshot" branch); all of
# them subscribe by field name, so the split is invisible to consumers.
*/
import type { AppState } from '../app-store';

export type SnapshotSlice = Pick<
  AppState,
  | 'snapshotPreview'
  | 'snapshotModalOpen'
  | 'snapshotPendingContent'
  | 'snapshotPendingTablesJson'
  | 'setSnapshotPreview'
  | 'openSnapshotModal'
  | 'closeSnapshotModal'
>;

type AppSet = (
  partial: Partial<AppState> | ((state: AppState) => Partial<AppState> | AppState),
) => void;

export function createSnapshotSlice(set: AppSet): SnapshotSlice {
  return {
    snapshotPreview: null,
    snapshotModalOpen: false,
    snapshotPendingContent: null,
    snapshotPendingTablesJson: null,

    // WHY: entering OR leaving a snapshot preview clears liveHeadings.
    // Why: liveHeadings is the CM6-derived override for the entity on screen, and a
    // snapshot swaps that content wholesale — keeping the previous headings would show the
    // live document's outline next to the snapshot's body (no-silent-degradation). The
    // write crosses into the headings concern on purpose: splitting it out would put half
    // of one transition in another file, which is how a reset gets forgotten.
    setSnapshotPreview: (cp) => set({ snapshotPreview: cp, liveHeadings: null }),

    openSnapshotModal: (pendingContent, pendingTablesJson = null) => set({
      snapshotModalOpen: true,
      snapshotPendingContent: pendingContent,
      snapshotPendingTablesJson: pendingTablesJson,
    }),
    closeSnapshotModal: () => set({
      snapshotModalOpen: false,
      snapshotPendingContent: null,
      snapshotPendingTablesJson: null,
    }),
  };
}

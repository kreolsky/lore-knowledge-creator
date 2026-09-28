// SYSTEM: chat-scope-tracker — "has the chat panel loaded this project's sessions?".
// Read by ChatPanel's mount effect to skip a redundant loadSessions; marked by
// openChatWithReference so the mount effect does NOT re-run loadSessions (which would
// restore the last-active chat and overwrite the null/ghost chat we just set).
let loadedProjectId: string | undefined;
export function markChatScopeLoaded(projectId: string): void { loadedProjectId = projectId; }
export function isChatScopeLoaded(projectId: string): boolean { return loadedProjectId === projectId; }
export function resetChatScopeTracker(): void { loadedProjectId = undefined; }

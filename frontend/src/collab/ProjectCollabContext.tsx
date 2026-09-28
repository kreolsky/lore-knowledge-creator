/** React context for the shared ProjectCollabConnection instance. */
// ARCH: One ProjectCollabConnection per project, created in ProjectPage, consumed via useProjectCollab().

import { createContext, useContext } from 'react';
import type { YjsProjectProvider } from './yjs-provider';

export const ProjectCollabContext = createContext<YjsProjectProvider | null>(null);

export function useProjectCollab(): YjsProjectProvider | null {
  return useContext(ProjectCollabContext);
}

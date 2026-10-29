import { describe, expect, it } from 'vitest';
import { canPickDigestSubjects } from './TranscriptionAgentConfigManager';
import type { AgentConfig } from '../../types';

const saved = (createdBy: string): AgentConfig => ({
  config_id: 'c1', document_id: 'theme', config_doc_id: 'cfg', target_doc_id: 'tgt', project_id: 'p',
  trigger_event: 'transcription_complete', title_template: null, model: null, kind: 'digest',
  created_by: createdBy, subject_user_ids: ['root'], created_at: '',
});

describe('canPickDigestSubjects', () => {

  it('refuses a non-root and a root on someone else\'s binding', () => {
    expect(canPickDigestSubjects(false, null, 'member')).toBe(false);
    expect(canPickDigestSubjects(true, saved('member'), 'root')).toBe(false);
  });
});

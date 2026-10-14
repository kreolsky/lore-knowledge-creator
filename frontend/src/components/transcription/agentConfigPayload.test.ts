import { describe, expect, it } from 'vitest';
import { agentConfigPayload, canEditDigestBinding, canPickDigestSubjects, needsAutosave } from './TranscriptionAgentConfigManager';
import type { AgentConfig } from '../../types';

const ROW = {
  key: 'row-1',
  config: null,
  kind: 'digest' as const,
  selectedConfigDocId: 'cfg',
  selectedTargetDocId: 'tgt',
  triggerEvent: 'transcription_complete',
  titleTemplate: '',
  modelOverride: '',
  subjectUserIds: ['root', 'member'],
  rev: 1,
  savedRev: 0,
  saving: false,
};

const saved = (createdBy: string): AgentConfig => ({
  config_id: 'c1', document_id: 'theme', config_doc_id: 'cfg', target_doc_id: 'tgt', project_id: 'p',
  trigger_event: 'transcription_complete', title_template: null, model: null, kind: 'digest',
  created_by: createdBy, subject_user_ids: ['root'], created_at: '',
});

describe('canPickDigestSubjects', () => {
  it('lets a root pick on a new binding or on their own', () => {
    expect(canPickDigestSubjects(true, null, 'root')).toBe(true);
    expect(canPickDigestSubjects(true, saved('root'), 'root')).toBe(true);
  });

  it('refuses a non-root and a root on someone else\'s binding', () => {
    expect(canPickDigestSubjects(false, null, 'member')).toBe(false);
    expect(canPickDigestSubjects(true, saved('member'), 'root')).toBe(false);
  });
});

describe('agentConfigPayload', () => {
  it('sends the subjects only when the viewer may pick them', () => {
    expect(agentConfigPayload('theme', ROW, true).subject_user_ids).toEqual(['root', 'member']);
    expect(agentConfigPayload('theme', ROW, false).subject_user_ids).toBeNull();
  });

  it('never sends subjects for an extractor, and always sends the kind', () => {
    const payload = agentConfigPayload('theme', { ...ROW, kind: 'extractor' }, true);
    expect(payload.kind).toBe('extractor');
    expect(payload.subject_user_ids).toBeNull();
  });

  it('sends an empty title template as null so the backend default applies', () => {
    expect(agentConfigPayload('theme', ROW, true).title_template).toBeNull();
  });
});

describe('canEditDigestBinding', () => {
  it('lets the author and a root change a saved digest', () => {
    expect(canEditDigestBinding(false, saved('member'), 'member')).toBe(true);
    expect(canEditDigestBinding(true, saved('member'), 'root')).toBe(true);
  });

  it('locks another member out of a saved digest, never a new row', () => {
    expect(canEditDigestBinding(false, saved('root'), 'member')).toBe(false);
    expect(canEditDigestBinding(false, null, 'member')).toBe(true);
  });
});

describe('needsAutosave', () => {
  it('saves an edited row once both documents are chosen', () => {
    expect(needsAutosave(ROW)).toBe(true);
  });

  it('waits for both documents, for an edit, and for a save in flight', () => {
    expect(needsAutosave({ ...ROW, selectedTargetDocId: null })).toBe(false);
    expect(needsAutosave({ ...ROW, savedRev: 1 })).toBe(false);
    expect(needsAutosave({ ...ROW, saving: true })).toBe(false);
  });
});

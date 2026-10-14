import { describe, expect, it } from 'vitest';
import { extractorModelOptions } from './TranscriptionAgentConfigManager';

const TOOLS = ['deepseek/flash', 'gemini/pro', 'local/orange/chat'];
const STRUCTURED = ['gemini/pro', 'local/orange/chat', 'openai/luna'];

describe('extractorModelOptions', () => {
  it('offers only utility models that declare structured outputs', () => {
    const values = extractorModelOptions(TOOLS, STRUCTURED, '', 'x').map(o => o.value);
    expect(values).toEqual(TOOLS.filter(m => STRUCTURED.includes(m)));
    // openai/luna is structured but not in this user's utility roster.
    expect(values).not.toContain('openai/luna');
  });

  it('keeps a saved unsupported model selectable, marked', () => {
    const options = extractorModelOptions(TOOLS, STRUCTURED, 'deepseek/flash', 'unsupported');
    expect(options.at(-1)).toEqual({ value: 'deepseek/flash', label: 'deepseek/flash — unsupported' });
  });

  it('does not duplicate a saved supported model', () => {
    const options = extractorModelOptions(TOOLS, STRUCTURED, 'gemini/pro', 'unsupported');
    expect(options.filter(o => o.value === 'gemini/pro')).toHaveLength(1);
  });
});

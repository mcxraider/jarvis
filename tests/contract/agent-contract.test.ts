// Contract/schema synchronization tests for the agent API.
//
// These tests validate shared JSON fixtures against the TypeScript Zod schemas
// to ensure the TS layer and Python layer agree on the API contract.
//
// NOTE: The default jest config only matches tests/unit/**/*.test.ts.
// Run this file with: npx jest --testPathPattern contract

import * as fs from 'fs';
import * as path from 'path';

import {
  AgentResponseSchema,
  AgentHealthDetailSchema,
  TelegramIdentitySchema,
  LangGraphInterruptSchema,
  StreamEventSchema,
  StreamFinalEventSchema,
  StreamReasoningSummaryEventSchema,
  StreamProgressEventSchema,
} from '../../src/types/agent.types';

const FIXTURES = path.join(__dirname, 'fixtures');

function loadFixture(name: string): unknown {
  const raw = fs.readFileSync(path.join(FIXTURES, name), 'utf-8');
  return JSON.parse(raw);
}

describe('Agent API contract — AgentResponseSchema', () => {
  it('accepts a completed response', () => {
    const data = loadFixture('response-completed.json');
    const result = AgentResponseSchema.safeParse(data);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.status).toBe('completed');
      expect(result.data.tool_results).toHaveLength(1);
    }
  });

  it('accepts an interrupted-confirm response with batch fields', () => {
    const data = loadFixture('response-interrupted-confirm.json');
    const result = AgentResponseSchema.safeParse(data);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.status).toBe('interrupted');
      expect(result.data.interrupt?.type).toBe('confirm');
      expect(result.data.interrupt?.tool_name).toBe('delete_todoist_task');
      expect(result.data.interrupt?.held_call_ids).toEqual(['held_abc']);
      expect(result.data.interrupt?.count).toBe(1);
      expect(result.data.interrupt?.tool_names).toEqual(['delete_todoist_task']);
      expect(result.data.interrupt?.services).toEqual(['todoist']);
    }
  });

  it('accepts an interrupted-clarify response', () => {
    const data = loadFixture('response-interrupted-clarify.json');
    const result = AgentResponseSchema.safeParse(data);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.status).toBe('interrupted');
      expect(result.data.interrupt?.type).toBe('clarify');
      expect(result.data.interrupt?.missing_fields).toEqual(['project_id']);
    }
  });

  it('accepts a failed response', () => {
    const data = loadFixture('response-failed.json');
    const result = AgentResponseSchema.safeParse(data);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.status).toBe('failed');
      expect(result.data.error).toContain('selected LLM provider');
      expect(result.data.error_details).toEqual(
        expect.objectContaining({ provider: 'openai', requested_model: 'gpt-5.6-luna' }),
      );
    }
  });
});

describe('Agent API contract — StreamEventSchema', () => {
  it('accepts a progress event', () => {
    const data = loadFixture('stream-progress.json');
    const result = StreamEventSchema.safeParse(data);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.type).toBe('progress');
    }
  });

  it('accepts a final event', () => {
    const data = loadFixture('stream-final.json');
    const result = StreamEventSchema.safeParse(data);
    expect(result.success).toBe(true);
    if (result.success && result.data.type === 'final') {
      expect(result.data.response.status).toBe('completed');
    }
  });

  it('accepts the shared reasoning_summary fixture', () => {
    const data = loadFixture('stream-reasoning-summary.json');
    const result = StreamReasoningSummaryEventSchema.safeParse(data);

    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data).toEqual({
        type: 'reasoning_summary',
        sequence: 2,
        text: 'I found the matching tasks and am checking their dates.',
      });
    }
  });

  it.each([
    ['missing text', { type: 'reasoning_summary', sequence: 2 }],
    ['missing sequence', { type: 'reasoning_summary', text: 'working' }],
    ['non-string text', { type: 'reasoning_summary', sequence: 2, text: 42 }],
    ['invalid type', { type: 'commentary', sequence: 2, text: 'working' }],
    ['negative sequence', { type: 'reasoning_summary', sequence: -1, text: 'working' }],
    ['zero sequence', { type: 'reasoning_summary', sequence: 0, text: 'working' }],
    ['fractional sequence', { type: 'reasoning_summary', sequence: 1.5, text: 'working' }],
  ])('rejects reasoning_summary with %s', (_label, data) => {
    expect(StreamReasoningSummaryEventSchema.safeParse(data).success).toBe(false);
  });

  it('StreamProgressEventSchema validates stage and message', () => {
    const data = loadFixture('stream-progress.json');
    const result = StreamProgressEventSchema.safeParse(data);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.stage).toBe('thinking');
      expect(result.data.message).toBe('Analyzing your request...');
      expect(result.data.sequence).toBe(1);
    }
  });

  it('StreamFinalEventSchema validates nested response', () => {
    const data = loadFixture('stream-final.json');
    const result = StreamFinalEventSchema.safeParse(data);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.response.thread_id).toBe('thread_abc123');
    }
  });
});

describe('Agent API contract — health detail', () => {
  it('exposes the non-secret runtime limits used by startup readiness', () => {
    const result = AgentHealthDetailSchema.safeParse(loadFixture('health-detail.json'));

    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.limits).toEqual({
        run_deadline_seconds: 150,
        max_agent_turns: 20,
        llm_request_timeout_seconds: 60,
        model_router_complex_timeout_seconds: 90,
      });
      expect(result.data.provider).toBe('openai');
    }
  });

  it('normalizes the rolling DeepSeek timeout alias to the provider-neutral field', () => {
    const legacy = {
      status: 'ok',
      model: 'deepseek-v4-flash',
      checks: { deepseek: { ok: true, detail: 'reachable' } },
      limits: {
        run_deadline_seconds: 150,
        max_agent_turns: 20,
        deepseek_request_timeout_seconds: 30,
        model_router_complex_timeout_seconds: 90,
      },
    };

    const result = AgentHealthDetailSchema.safeParse(legacy);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.provider).toBe('deepseek');
      expect(result.data.limits).toEqual({
        run_deadline_seconds: 150,
        max_agent_turns: 20,
        llm_request_timeout_seconds: 30,
        model_router_complex_timeout_seconds: 90,
      });
      expect(result.data.limits).not.toHaveProperty('deepseek_request_timeout_seconds');
    }
  });

  it.each([
    ['missing request timeout', undefined],
    ['zero request timeout', 0],
    ['negative request timeout', -1],
    ['non-finite request timeout', Number.POSITIVE_INFINITY],
  ])('rejects health limits with %s', (_label, llmTimeout) => {
    const health = loadFixture('health-detail.json') as Record<string, any>;
    if (llmTimeout === undefined) {
      delete health.limits.llm_request_timeout_seconds;
    } else {
      health.limits.llm_request_timeout_seconds = llmTimeout;
    }
    expect(AgentHealthDetailSchema.safeParse(health).success).toBe(false);
  });
});

describe('Agent API contract — rejection cases', () => {
  it('rejects response missing status', () => {
    const data = { thread_id: 'thread_1', response: 'hi' };
    const result = AgentResponseSchema.safeParse(data);
    expect(result.success).toBe(false);
  });

  it('rejects response with unknown status value', () => {
    const data = { status: 'pending', thread_id: 'thread_1', response: 'hi' };
    const result = AgentResponseSchema.safeParse(data);
    expect(result.success).toBe(false);
  });

  it('rejects response missing thread_id', () => {
    const data = { status: 'completed', response: 'hi' };
    const result = AgentResponseSchema.safeParse(data);
    expect(result.success).toBe(false);
  });

  it('rejects progress events with an invalid semantic fact', () => {
    const data = { type: 'progress', fact: { phase: 'unknown', action: 'started' } };
    const result = StreamProgressEventSchema.safeParse(data);
    expect(result.success).toBe(false);
  });

  it('accepts progress event with only stage (legacy format)', () => {
    const data = { type: 'progress', stage: 'thinking' };
    const result = StreamProgressEventSchema.safeParse(data);
    expect(result.success).toBe(true);
  });
});

describe('Agent API contract — InvokeRequest fixture', () => {
  it('invoke-request fixture has all required fields', () => {
    const data = loadFixture('invoke-request.json') as Record<string, unknown>;
    // Required fields for InvokeRequest
    expect(data).toHaveProperty('message');
    expect(data).toHaveProperty('user_id');
    expect(typeof data.message).toBe('string');
    expect(typeof data.user_id).toBe('string');
    expect((data.message as string).length).toBeGreaterThan(0);
    expect((data.user_id as string).length).toBeGreaterThan(0);
    expect(TelegramIdentitySchema.safeParse(data.telegram_identity).success).toBe(true);
  });

  it('resume-request fixture has all required fields', () => {
    const data = loadFixture('resume-request.json') as Record<string, unknown>;
    // Required fields for ResumeRequest
    expect(data).toHaveProperty('message');
    expect(data).toHaveProperty('user_id');
    expect(data).toHaveProperty('thread_id');
    expect(typeof data.message).toBe('string');
    expect(typeof data.user_id).toBe('string');
    expect(typeof data.thread_id).toBe('string');
    expect((data.message as string).length).toBeGreaterThan(0);
    expect((data.user_id as string).length).toBeGreaterThan(0);
    expect((data.thread_id as string).length).toBeGreaterThan(0);
    expect(TelegramIdentitySchema.safeParse(data.telegram_identity).success).toBe(true);
  });
});

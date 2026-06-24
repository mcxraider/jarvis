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
  LangGraphInterruptSchema,
  StreamEventSchema,
  StreamFinalEventSchema,
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

  it('accepts an interrupted-confirm response', () => {
    const data = loadFixture('response-interrupted-confirm.json');
    const result = AgentResponseSchema.safeParse(data);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.status).toBe('interrupted');
      expect(result.data.interrupt?.type).toBe('confirm');
      expect(result.data.interrupt?.tool_name).toBe('delete_todoist_task');
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
      expect(result.data.error).toContain('DeepSeek');
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

  it('rejects progress event without stage', () => {
    const data = { type: 'progress', message: 'Working...' };
    const result = StreamProgressEventSchema.safeParse(data);
    expect(result.success).toBe(false);
  });

  it('rejects progress event without message', () => {
    const data = { type: 'progress', stage: 'thinking' };
    const result = StreamProgressEventSchema.safeParse(data);
    expect(result.success).toBe(false);
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
  });
});

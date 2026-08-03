// src/types/agent.types.ts — Zod schemas and inferred TypeScript types for the
// LangGraph agent API contract. These schemas validate responses from the Python
// backend and define the streaming event protocol (progress + final payloads).

import { z } from 'zod';

export const TelegramIdentitySchema = z.object({
  telegram_id: z.number().int().positive(),
  username: z.string().optional(),
});

// Interrupt metadata sent when the agent pauses for human input. Contains the
// interrupt type (clarify or confirm) and context about what the agent needs.
export const LangGraphInterruptSchema = z.object({
  type: z.enum(['clarify', 'confirm']).optional(),
  question: z.string().optional(),
  reason: z.string().optional(),
  missing_fields: z.array(z.string()).optional(),
  risk: z.string().optional(),
  tool_call_id: z.string().optional(),
  thread_id: z.string().optional(),
  user_id: z.string().optional(),
  request_source: z.string().optional(),
  held_call_id: z.string().optional(),
  summary: z.string().optional(),
  tool_name: z.string().optional(),
  args: z.record(z.unknown()).optional(),
  // Batch confirm fields (multi-call confirmation payloads from Python confirm node)
  held_call_ids: z.array(z.string()).optional(),
  count: z.number().int().positive().optional(),
  tool_names: z.array(z.string()).optional(),
  services: z.array(z.string()).optional(),
});

// The complete response from /invoke or /resume (or the "final" stream event payload).
export const AgentResponseSchema = z.object({
  status: z.enum(['completed', 'interrupted', 'failed']),
  thread_id: z.string(),
  response: z.string(),
  reasoning_content: z.string().nullish(),
  interrupt: LangGraphInterruptSchema.nullish(),
  tool_results: z.array(z.record(z.unknown())).nullish(),
  error: z.string().nullish(),
  error_details: z.record(z.unknown()).nullish(),
});

export const AgentDependencyCheckSchema = z.object({
  ok: z.boolean(),
  detail: z.string(),
});

const AgentRuntimeLimitsInputSchema = z
  .object({
    run_deadline_seconds: z.number().finite().positive(),
    max_agent_turns: z.number().int().positive(),
    llm_request_timeout_seconds: z.number().finite().positive().optional(),
    // One-release rolling-upgrade alias. Parsed output is normalized to the
    // provider-neutral field below, so callers never need provider branching.
    deepseek_request_timeout_seconds: z.number().finite().positive().optional(),
    model_router_complex_timeout_seconds: z.number().finite().positive(),
  })
  .superRefine((limits, context) => {
    if (
      limits.llm_request_timeout_seconds === undefined &&
      limits.deepseek_request_timeout_seconds === undefined
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['llm_request_timeout_seconds'],
        message: 'LLM request timeout is required',
      });
    }
  });

export const AgentRuntimeLimitsSchema = AgentRuntimeLimitsInputSchema.transform(
  ({ deepseek_request_timeout_seconds, llm_request_timeout_seconds, ...limits }) => ({
    ...limits,
    llm_request_timeout_seconds:
      llm_request_timeout_seconds ?? (deepseek_request_timeout_seconds as number),
  }),
);

export const AgentHealthDetailSchema = z.object({
  status: z.enum(['ok', 'degraded']),
  // Older DeepSeek-only backends omitted this field. Defaulting preserves
  // rolling compatibility while upgraded backends report it explicitly.
  provider: z.enum(['deepseek', 'openai']).optional().default('deepseek'),
  model: z.string(),
  checks: z.record(AgentDependencyCheckSchema),
  // Optional so /status remains compatible with a backend during a rolling upgrade.
  // Startup readiness separately requires this block before accepting webhooks.
  limits: AgentRuntimeLimitsSchema.optional(),
});

export const ProgressDomainSchema = z.enum(['todoist', 'calendar', 'gmail', 'notion']);
export const ProgressFactSchema = z.object({
  phase: z.enum([
    'request',
    'routing',
    'lookup',
    'review',
    'preparing_change',
    'awaiting_confirmation',
    'applying_change',
    'finalizing',
    'retrying',
    'failed',
  ]),
  action: z.enum(['started', 'completed', 'waiting', 'retrying', 'failed']),
  domains: z.array(ProgressDomainSchema).nullish(),
  intent: z.enum(['read', 'mutation', 'clarify', 'confirm']).optional(),
  retry: z
    .object({
      target: z.enum(['domain', 'model', 'router']).optional(),
      domain: ProgressDomainSchema.optional(),
      reason: z.enum(['temporary_connection', 'rate_limited', 'service_unavailable', 'timeout']),
    })
    .optional(),
});

// Streaming protocol: each line of the NDJSON stream is either a progress event
// (stage update for the UI) or a final event (complete agent response payload).
export const StreamProgressEventSchema = z.object({
  type: z.literal('progress'),
  // Legacy stage-only progress remains accepted, but sequenced native events
  // must use the positive monotonic counter emitted by the Python stream.
  sequence: z.number().int().positive().optional(),
  // Legacy fields remain accepted while clients migrate to fact-based progress.
  stage: z.string().optional(),
  message: z.string().optional(),
  fact: ProgressFactSchema.optional(),
  metadata: z.record(z.unknown()).optional(),
});

export const StreamNarrationEventSchema = z.object({
  type: z.literal('narration'),
  sequence: z.number().int().positive(),
  text: z.string(),
});

export const StreamFinalEventSchema = z.object({
  type: z.literal('final'),
  response: AgentResponseSchema,
});

export const StreamEventSchema = z.discriminatedUnion('type', [
  StreamProgressEventSchema,
  StreamNarrationEventSchema,
  StreamFinalEventSchema,
]);

export type LangGraphInterrupt = z.infer<typeof LangGraphInterruptSchema>;
export type TelegramIdentityPayload = z.infer<typeof TelegramIdentitySchema>;
export type AgentResponse = z.infer<typeof AgentResponseSchema>;
export type AgentDependencyCheck = z.infer<typeof AgentDependencyCheckSchema>;
export type AgentRuntimeLimits = z.infer<typeof AgentRuntimeLimitsSchema>;
export type AgentHealthDetail = z.infer<typeof AgentHealthDetailSchema>;
export type ProgressFact = z.infer<typeof ProgressFactSchema>;
export type StreamProgressEvent = z.infer<typeof StreamProgressEventSchema>;
export type StreamNarrationEvent = z.infer<typeof StreamNarrationEventSchema>;
export type StreamFinalEvent = z.infer<typeof StreamFinalEventSchema>;
export type StreamEvent = z.infer<typeof StreamEventSchema>;

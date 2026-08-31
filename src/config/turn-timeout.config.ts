// Code-owned timeout ladder for one Telegram agent turn.
//
// The Python run deadline is reported by /health/detail and must remain below
// these outer watchdogs. Environment variables are intentionally supported as
// emergency/test overrides, but the deployment templates do not advertise them
// as routine tuning knobs.
//
// Ordering, innermost to outermost:
//
//   streamIdleMs  <  overallMs  <  telegrafHandlerMs  <  runningGateTtlMs  <=  waitingGateTtlMs
//
// A long voice note adds a transcription stage *before* the agent turn:
// FFmpeg normalization plus chunk extraction (`audioPrepareMs`) followed by the
// long-form Groq transcription stage (`audioTranscriptionMs`). Both run inside
// the same Telegraf handler invocation as the agent turn, so their sum must stay
// below `telegrafHandlerMs`.
//
// The conversation-gate TTL is the outermost bound on purpose: the gate is what
// serializes a conversation, and it is reclaimed by TTL rather than by the
// handler returning. If the running gate TTL expired before the handler did, a
// still-working turn would lose ownership of its own conversation and a second
// message could start a concurrent turn against the same thread.

export interface LangGraphClientTimeoutConfig {
  overallMs: number;
  streamIdleMs: number;
}

export interface TurnTimeoutConfig extends LangGraphClientTimeoutConfig {
  telegrafHandlerMs: number;
  /** Wall-clock budget for FFmpeg normalization + chunk extraction. */
  audioPrepareMs: number;
  /** Wall-clock budget for the whole long-form Groq transcription stage. */
  audioTranscriptionMs: number;
  /** Conversation-gate TTL while a turn is running. Must outlast the Telegraf handler. */
  runningGateTtlMs: number;
  /** Conversation-gate TTL while waiting on the user (HITL). */
  waitingGateTtlMs: number;
}

export interface TurnTimeoutOverrides {
  timeoutMs?: number;
  streamIdleTimeoutMs?: number;
  handlerTimeoutMs?: number;
  audioPrepareMs?: number;
  audioTranscriptionMs?: number;
  runningGateTtlMs?: number;
  waitingGateTtlMs?: number;
}

export interface TurnTimeoutLadderViolation {
  rule: string;
  detail: string;
}

export const TURN_TIMEOUT_DEFAULTS: Readonly<TurnTimeoutConfig> = Object.freeze({
  overallMs: 165_000,
  streamIdleMs: 155_000,
  telegrafHandlerMs: 600_000,
  audioPrepareMs: 120_000,
  audioTranscriptionMs: 360_000,
  runningGateTtlMs: 720_000,
  waitingGateTtlMs: 1_800_000,
});

export function resolveLangGraphClientTimeouts(
  overrides: Pick<TurnTimeoutOverrides, 'timeoutMs' | 'streamIdleTimeoutMs'> = {},
  env: NodeJS.ProcessEnv = process.env,
): LangGraphClientTimeoutConfig {
  return {
    overallMs: resolveTimeoutMs(
      overrides.timeoutMs,
      env.LANGGRAPH_AGENT_TIMEOUT_MS,
      TURN_TIMEOUT_DEFAULTS.overallMs,
      'timeoutMs',
      'LANGGRAPH_AGENT_TIMEOUT_MS',
    ),
    streamIdleMs: resolveTimeoutMs(
      overrides.streamIdleTimeoutMs,
      env.LANGGRAPH_STREAM_IDLE_TIMEOUT_MS,
      TURN_TIMEOUT_DEFAULTS.streamIdleMs,
      'streamIdleTimeoutMs',
      'LANGGRAPH_STREAM_IDLE_TIMEOUT_MS',
    ),
  };
}

export function resolveTelegrafHandlerTimeoutMs(
  explicitValue?: number,
  env: NodeJS.ProcessEnv = process.env,
): number {
  return resolveTimeoutMs(
    explicitValue,
    env.TELEGRAM_HANDLER_TIMEOUT_MS,
    TURN_TIMEOUT_DEFAULTS.telegrafHandlerMs,
    'handlerTimeoutMs',
    'TELEGRAM_HANDLER_TIMEOUT_MS',
  );
}

export function resolveAudioPrepareTimeoutMs(
  explicitValue?: number,
  env: NodeJS.ProcessEnv = process.env,
): number {
  return resolveTimeoutMs(
    explicitValue,
    env.GROQ_AUDIO_PREPARE_TIMEOUT_MS,
    TURN_TIMEOUT_DEFAULTS.audioPrepareMs,
    'audioPrepareMs',
    'GROQ_AUDIO_PREPARE_TIMEOUT_MS',
  );
}

export function resolveAudioTranscriptionTimeoutMs(
  explicitValue?: number,
  env: NodeJS.ProcessEnv = process.env,
): number {
  return resolveTimeoutMs(
    explicitValue,
    env.GROQ_AUDIO_TRANSCRIPTION_TIMEOUT_MS,
    TURN_TIMEOUT_DEFAULTS.audioTranscriptionMs,
    'audioTranscriptionMs',
    'GROQ_AUDIO_TRANSCRIPTION_TIMEOUT_MS',
  );
}

export function resolveRunningGateTtlMs(
  explicitValue?: number,
  env: NodeJS.ProcessEnv = process.env,
): number {
  return resolveTimeoutMs(
    explicitValue,
    env.TELEGRAM_GATE_RUNNING_TTL_MS,
    TURN_TIMEOUT_DEFAULTS.runningGateTtlMs,
    'runningGateTtlMs',
    'TELEGRAM_GATE_RUNNING_TTL_MS',
  );
}

export function resolveWaitingGateTtlMs(
  explicitValue?: number,
  env: NodeJS.ProcessEnv = process.env,
): number {
  return resolveTimeoutMs(
    explicitValue,
    env.TELEGRAM_GATE_WAITING_TTL_MS,
    TURN_TIMEOUT_DEFAULTS.waitingGateTtlMs,
    'waitingGateTtlMs',
    'TELEGRAM_GATE_WAITING_TTL_MS',
  );
}

export function resolveTurnTimeoutConfig(
  overrides: TurnTimeoutOverrides = {},
  env: NodeJS.ProcessEnv = process.env,
): TurnTimeoutConfig {
  const langGraph = resolveLangGraphClientTimeouts(overrides, env);
  const config: TurnTimeoutConfig = {
    ...langGraph,
    telegrafHandlerMs: resolveTelegrafHandlerTimeoutMs(overrides.handlerTimeoutMs, env),
    audioPrepareMs: resolveAudioPrepareTimeoutMs(overrides.audioPrepareMs, env),
    audioTranscriptionMs: resolveAudioTranscriptionTimeoutMs(overrides.audioTranscriptionMs, env),
    runningGateTtlMs: resolveRunningGateTtlMs(overrides.runningGateTtlMs, env),
    waitingGateTtlMs: resolveWaitingGateTtlMs(overrides.waitingGateTtlMs, env),
  };
  assertTurnTimeoutLadder(config);
  return config;
}

export function findTurnTimeoutLadderViolations(
  config: TurnTimeoutConfig,
): TurnTimeoutLadderViolation[] {
  const violations: TurnTimeoutLadderViolation[] = [];

  if (!(config.streamIdleMs < config.overallMs)) {
    violations.push({
      rule: 'streamIdleMs < overallMs',
      detail: `streamIdleMs ${config.streamIdleMs}ms must be below overallMs ${config.overallMs}ms`,
    });
  }
  if (!(config.overallMs < config.telegrafHandlerMs)) {
    violations.push({
      rule: 'overallMs < telegrafHandlerMs',
      detail: `overallMs ${config.overallMs}ms must be below telegrafHandlerMs ${config.telegrafHandlerMs}ms`,
    });
  }
  const audioStageMs = config.audioPrepareMs + config.audioTranscriptionMs;
  if (!(audioStageMs < config.telegrafHandlerMs)) {
    violations.push({
      rule: 'audioPrepareMs + audioTranscriptionMs < telegrafHandlerMs',
      detail: `audio stage ${audioStageMs}ms (prepare ${config.audioPrepareMs}ms + transcription ${config.audioTranscriptionMs}ms) must be below telegrafHandlerMs ${config.telegrafHandlerMs}ms`,
    });
  }
  if (!(config.telegrafHandlerMs < config.runningGateTtlMs)) {
    violations.push({
      rule: 'telegrafHandlerMs < runningGateTtlMs',
      detail: `telegrafHandlerMs ${config.telegrafHandlerMs}ms must be below runningGateTtlMs ${config.runningGateTtlMs}ms`,
    });
  }
  if (!(config.runningGateTtlMs <= config.waitingGateTtlMs)) {
    violations.push({
      rule: 'runningGateTtlMs <= waitingGateTtlMs',
      detail: `runningGateTtlMs ${config.runningGateTtlMs}ms must not exceed waitingGateTtlMs ${config.waitingGateTtlMs}ms`,
    });
  }

  return violations;
}

export function assertTurnTimeoutLadder(config: TurnTimeoutConfig): void {
  const violations = findTurnTimeoutLadderViolations(config);
  if (violations.length > 0) {
    const summary = violations.map(({ rule, detail }) => `${rule}: ${detail}`).join('; ');
    throw new Error(`Turn timeout ladder inverted: ${summary}`);
  }
}

function resolveTimeoutMs(
  explicitValue: number | undefined,
  environmentValue: string | undefined,
  defaultValue: number,
  explicitName: string,
  environmentName: string,
): number {
  const fromEnvironment = environmentValue === undefined ? undefined : Number(environmentValue);
  const value = explicitValue ?? fromEnvironment ?? defaultValue;
  const sourceName =
    explicitValue !== undefined
      ? explicitName
      : environmentValue !== undefined
        ? environmentName
        : explicitName;

  if (!Number.isFinite(value) || value <= 0) {
    throw new Error(`${sourceName} must be finite and greater than zero`);
  }
  return value;
}

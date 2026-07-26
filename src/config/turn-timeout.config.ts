// Code-owned timeout ladder for one Telegram agent turn.
//
// The Python run deadline is reported by /health/detail and must remain below
// these outer watchdogs. Environment variables are intentionally supported as
// emergency/test overrides, but the deployment templates do not advertise them
// as routine tuning knobs.

export interface LangGraphClientTimeoutConfig {
  overallMs: number;
  streamIdleMs: number;
}

export interface TurnTimeoutConfig extends LangGraphClientTimeoutConfig {
  telegrafHandlerMs: number;
}

export interface TurnTimeoutOverrides {
  timeoutMs?: number;
  streamIdleTimeoutMs?: number;
  handlerTimeoutMs?: number;
}

export const TURN_TIMEOUT_DEFAULTS: Readonly<TurnTimeoutConfig> = Object.freeze({
  overallMs: 165_000,
  streamIdleMs: 155_000,
  telegrafHandlerMs: 195_000,
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

export function resolveTurnTimeoutConfig(
  overrides: TurnTimeoutOverrides = {},
  env: NodeJS.ProcessEnv = process.env,
): TurnTimeoutConfig {
  const langGraph = resolveLangGraphClientTimeouts(overrides, env);
  return {
    ...langGraph,
    telegrafHandlerMs: resolveTelegrafHandlerTimeoutMs(overrides.handlerTimeoutMs, env),
  };
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

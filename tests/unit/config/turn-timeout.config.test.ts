import {
  resolveLangGraphClientTimeouts,
  resolveTelegrafHandlerTimeoutMs,
  resolveTurnTimeoutConfig,
  TURN_TIMEOUT_DEFAULTS,
} from '../../../src/config/turn-timeout.config';

describe('turn timeout configuration', () => {
  it('uses the code-owned ladder defaults when no overrides are present', () => {
    expect(resolveTurnTimeoutConfig({}, {})).toEqual({
      overallMs: 165_000,
      streamIdleMs: 155_000,
      telegrafHandlerMs: 195_000,
    });
    expect(TURN_TIMEOUT_DEFAULTS).toEqual({
      overallMs: 165_000,
      streamIdleMs: 155_000,
      telegrafHandlerMs: 195_000,
    });
  });

  it('accepts optional environment overrides for emergency and test use', () => {
    const env = {
      LANGGRAPH_AGENT_TIMEOUT_MS: '180000',
      LANGGRAPH_STREAM_IDLE_TIMEOUT_MS: '130000',
      TELEGRAM_HANDLER_TIMEOUT_MS: '210000',
    };

    expect(resolveTurnTimeoutConfig({}, env)).toEqual({
      overallMs: 180_000,
      streamIdleMs: 130_000,
      telegrafHandlerMs: 210_000,
    });
  });

  it('gives explicit constructor and service values precedence over environment overrides', () => {
    const invalidEnvironment = {
      LANGGRAPH_AGENT_TIMEOUT_MS: 'invalid',
      LANGGRAPH_STREAM_IDLE_TIMEOUT_MS: '0',
      TELEGRAM_HANDLER_TIMEOUT_MS: '-1',
    };

    expect(resolveTurnTimeoutConfig({
      timeoutMs: 170_000,
      streamIdleTimeoutMs: 125_000,
      handlerTimeoutMs: 200_000,
    }, invalidEnvironment)).toEqual({
      overallMs: 170_000,
      streamIdleMs: 125_000,
      telegrafHandlerMs: 200_000,
    });
  });

  it.each([
    ['LANGGRAPH_AGENT_TIMEOUT_MS', '0'],
    ['LANGGRAPH_AGENT_TIMEOUT_MS', '-1'],
    ['LANGGRAPH_AGENT_TIMEOUT_MS', 'nan'],
    ['LANGGRAPH_AGENT_TIMEOUT_MS', 'Infinity'],
    ['LANGGRAPH_STREAM_IDLE_TIMEOUT_MS', '0'],
    ['LANGGRAPH_STREAM_IDLE_TIMEOUT_MS', '-1'],
    ['LANGGRAPH_STREAM_IDLE_TIMEOUT_MS', 'nan'],
    ['LANGGRAPH_STREAM_IDLE_TIMEOUT_MS', 'Infinity'],
    ['TELEGRAM_HANDLER_TIMEOUT_MS', '0'],
    ['TELEGRAM_HANDLER_TIMEOUT_MS', '-1'],
    ['TELEGRAM_HANDLER_TIMEOUT_MS', 'nan'],
    ['TELEGRAM_HANDLER_TIMEOUT_MS', 'Infinity'],
  ])('rejects invalid override %s=%s', (name, value) => {
    expect(() => resolveTurnTimeoutConfig({}, { [name]: value })).toThrow(
      `${name} must be finite and greater than zero`,
    );
  });

  it('lets standalone owners resolve only their part of the ladder', () => {
    expect(resolveLangGraphClientTimeouts({}, {})).toEqual({
      overallMs: 165_000,
      streamIdleMs: 155_000,
    });
    expect(resolveTelegrafHandlerTimeoutMs(undefined, {})).toBe(195_000);
  });
});

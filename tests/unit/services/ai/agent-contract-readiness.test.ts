import {
  AgentContractTimeouts,
  verifyAgentContract,
} from '../../../../src/services/ai/agent-contract-readiness';
import { logger } from '../../../../src/utils/logger';

const validHealth = {
  status: 'ok' as const,
  provider: 'openai' as const,
  model: 'gpt-5.6-luna',
  checks: {},
  limits: {
    run_deadline_seconds: 150,
    max_agent_turns: 20,
    llm_request_timeout_seconds: 60,
    model_router_complex_timeout_seconds: 90,
  },
};

// Production ladder: 165s client overall / 155s idle / 600s handler,
// 120s audio prepare + 360s transcription, 720s running gate TTL.
const validTimeouts: AgentContractTimeouts = {
  clientOverallMs: 165_000,
  clientIdleMs: 155_000,
  telegrafHandlerTimeoutMs: 600_000,
  audioPrepareMs: 120_000,
  audioTranscriptionMs: 360_000,
  runningGateTtlMs: 720_000,
};

describe('verifyAgentContract', () => {
  it('accepts and logs the live ordered timeout ladder', async () => {
    const client = { fetchDependencyHealth: jest.fn().mockResolvedValue(validHealth) };

    await expect(verifyAgentContract(client as any, validTimeouts)).resolves.toEqual({
      verified: true,
      limits: validHealth.limits,
    });
    expect(logger.info).toHaveBeenCalledWith(
      'agent.contract.verified',
      expect.objectContaining({
        runDeadlineMs: 150_000,
        llmRequestTimeoutMs: 60_000,
        clientOverallMs: 165_000,
        telegrafHandlerTimeoutMs: 600_000,
      }),
    );
  });

  it('logs the audio and gate budgets on the verified contract', async () => {
    const client = { fetchDependencyHealth: jest.fn().mockResolvedValue(validHealth) };

    await expect(verifyAgentContract(client as any, validTimeouts)).resolves.toEqual({
      verified: true,
      limits: validHealth.limits,
    });
    expect(logger.info).toHaveBeenCalledWith(
      'agent.contract.verified',
      expect.objectContaining({
        audioPrepareMs: 120_000,
        audioTranscriptionMs: 360_000,
        audioStageMs: 480_000,
        runningGateTtlMs: 720_000,
      }),
    );
  });

  it.each([
    [
      'complex timeout equals idle timeout',
      validTimeouts,
      { model_router_complex_timeout_seconds: 155 },
    ],
    ['run deadline equals client idle', { ...validTimeouts, clientIdleMs: 150_000 }, {}],
    ['run deadline equals client overall', validTimeouts, { run_deadline_seconds: 165 }],
    [
      'client overall exceeds Telegraf watchdog',
      { ...validTimeouts, telegrafHandlerTimeoutMs: 160_000 },
      {},
    ],
  ])('throws when %s', async (_label, timeouts, limitsOverride) => {
    const client = {
      fetchDependencyHealth: jest.fn().mockResolvedValue({
        ...validHealth,
        limits: { ...validHealth.limits, ...limitsOverride },
      }),
    };

    await expect(verifyAgentContract(client as any, timeouts)).rejects.toThrow(
      'Agent timeout contract inverted',
    );
  });

  it.each([
    ['the audio stage exceeds the Telegraf watchdog', { audioTranscriptionMs: 500_000 }],
    ['the audio stage exactly fills the Telegraf watchdog', { audioTranscriptionMs: 480_000 }],
  ])('throws when %s', async (_label, patch) => {
    const client = { fetchDependencyHealth: jest.fn().mockResolvedValue(validHealth) };

    await expect(
      verifyAgentContract(client as any, { ...validTimeouts, ...patch }),
    ).rejects.toThrow(
      /Agent timeout contract inverted: audio stage \d+ms \(prepare 120000ms \+ transcription \d+ms\) must be below Telegraf handler timeout 600000ms/,
    );
  });

  it.each([
    ['the Telegraf watchdog outlives the running gate TTL', { runningGateTtlMs: 500_000 }],
    ['the Telegraf watchdog equals the running gate TTL', { runningGateTtlMs: 600_000 }],
  ])('throws when %s', async (_label, patch) => {
    const client = { fetchDependencyHealth: jest.fn().mockResolvedValue(validHealth) };

    await expect(
      verifyAgentContract(client as any, { ...validTimeouts, ...patch }),
    ).rejects.toThrow(
      /Agent timeout contract inverted: Telegraf handler timeout 600000ms must be below running conversation-gate TTL \d+ms/,
    );
  });

  it('lists every inverted bound in one thrown message', async () => {
    const client = { fetchDependencyHealth: jest.fn().mockResolvedValue(validHealth) };

    let message = '';
    try {
      await verifyAgentContract(client as any, {
        clientOverallMs: 100_000,
        clientIdleMs: 90_000,
        telegrafHandlerTimeoutMs: 95_000,
        audioPrepareMs: 60_000,
        audioTranscriptionMs: 60_000,
        runningGateTtlMs: 90_000,
      });
    } catch (error) {
      message = error instanceof Error ? error.message : String(error);
    }

    expect(message).toContain('Agent timeout contract inverted:');
    expect(message).toContain('complex model timeout');
    expect(message).toContain('run deadline');
    expect(message).toContain('client overall timeout');
    expect(message).toContain('audio stage');
    expect(message).toContain('running conversation-gate TTL');
  });

  it('throws when a reachable backend omits the limits contract', async () => {
    const client = {
      fetchDependencyHealth: jest.fn().mockResolvedValue({ ...validHealth, limits: undefined }),
    };

    await expect(verifyAgentContract(client as any, validTimeouts)).rejects.toThrow(
      '/health/detail omitted runtime limits',
    );
  });

  it.each([0, -1, Number.NaN, Number.POSITIVE_INFINITY])(
    'rejects an invalid provider-neutral request timeout: %s',
    async (llmRequestTimeout) => {
      const client = {
        fetchDependencyHealth: jest.fn().mockResolvedValue({
          ...validHealth,
          limits: {
            ...validHealth.limits,
            llm_request_timeout_seconds: llmRequestTimeout,
          },
        }),
      };

      // The real client rejects this payload through Zod before readiness runs.
      // Keep this assertion at the readiness seam by requiring a finite positive
      // limit before it can be treated as verified.
      await expect(verifyAgentContract(client as any, validTimeouts)).rejects.toThrow();
    },
  );

  it('logs at error and continues when the backend is unreachable', async () => {
    const client = {
      fetchDependencyHealth: jest.fn().mockRejectedValue(new Error('connect ECONNREFUSED')),
    };

    await expect(verifyAgentContract(client as any, validTimeouts)).resolves.toEqual({
      verified: false,
    });
    expect(logger.error).toHaveBeenCalledWith(
      'agent.contract.unverified',
      expect.objectContaining({ error: 'connect ECONNREFUSED' }),
    );
  });
});

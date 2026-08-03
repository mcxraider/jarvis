import { verifyAgentContract } from '../../../../src/services/ai/agent-contract-readiness';
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

const validTimeouts = {
  clientOverallMs: 165_000,
  clientIdleMs: 155_000,
  telegrafHandlerTimeoutMs: 195_000,
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
        telegrafHandlerTimeoutMs: 195_000,
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

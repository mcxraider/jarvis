import { verifyAgentContract } from '../../../../src/services/ai/agent-contract-readiness';
import { logger } from '../../../../src/utils/logger';

const validHealth = {
  status: 'ok' as const,
  model: 'deepseek-v4-flash',
  checks: {},
  limits: {
    run_deadline_seconds: 150,
    max_agent_turns: 20,
    deepseek_request_timeout_seconds: 30,
    model_router_complex_timeout_seconds: 90,
  },
};

const validTimeouts = {
  clientOverallMs: 165_000,
  clientIdleMs: 120_000,
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
        clientOverallMs: 165_000,
        telegrafHandlerTimeoutMs: 195_000,
      }),
    );
  });

  it.each([
    ['complex timeout equals idle timeout', validTimeouts, { model_router_complex_timeout_seconds: 120 }],
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

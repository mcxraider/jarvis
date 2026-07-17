import { logger } from '../../utils/logger';
import {
  LangGraphAgentClient,
  LangGraphDependencyHealth,
} from './langgraph-agent-client.service';

export interface AgentContractTimeouts {
  clientOverallMs: number;
  clientIdleMs: number;
  telegrafHandlerTimeoutMs: number;
}

export interface AgentContractReadiness {
  verified: boolean;
  limits?: NonNullable<LangGraphDependencyHealth['limits']>;
}

type AgentHealthClient = Pick<LangGraphAgentClient, 'fetchDependencyHealth'>;

export async function verifyAgentContract(
  client: AgentHealthClient,
  timeouts: AgentContractTimeouts,
): Promise<AgentContractReadiness> {
  let health: LangGraphDependencyHealth;
  try {
    health = await client.fetchDependencyHealth();
  } catch (error) {
    logger.error('agent.contract.unverified', {
      error: error instanceof Error ? error.message : String(error),
      ...timeouts,
    });
    return { verified: false };
  }

  const limits = health.limits;
  if (!limits) {
    throw new Error('Agent contract readiness failed: /health/detail omitted runtime limits');
  }

  const runDeadlineMs = limits.run_deadline_seconds * 1000;
  const complexModelTimeoutMs = limits.model_router_complex_timeout_seconds * 1000;
  const violations: string[] = [];

  if (!(complexModelTimeoutMs < timeouts.clientIdleMs)) {
    violations.push(
      `complex model timeout ${complexModelTimeoutMs}ms must be below client idle timeout ${timeouts.clientIdleMs}ms`,
    );
  }
  if (!(runDeadlineMs < timeouts.clientOverallMs)) {
    violations.push(
      `run deadline ${runDeadlineMs}ms must be below client overall timeout ${timeouts.clientOverallMs}ms`,
    );
  }
  if (!(timeouts.clientOverallMs < timeouts.telegrafHandlerTimeoutMs)) {
    violations.push(
      `client overall timeout ${timeouts.clientOverallMs}ms must be below Telegraf handler timeout ${timeouts.telegrafHandlerTimeoutMs}ms`,
    );
  }

  if (violations.length > 0) {
    throw new Error(`Agent timeout contract inverted: ${violations.join('; ')}`);
  }

  logger.info('agent.contract.verified', {
    runDeadlineMs,
    complexModelTimeoutMs,
    maxAgentTurns: limits.max_agent_turns,
    deepseekRequestTimeoutMs: limits.deepseek_request_timeout_seconds * 1000,
    ...timeouts,
  });
  return { verified: true, limits };
}

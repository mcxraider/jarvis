import { BotActivityService } from '../../../../src/services/telegram/bot-activity.service';
import {
  AgentDependencyHealth,
  BotStatusService,
} from '../../../../src/services/telegram/bot-status.service';

describe('BotStatusService', () => {
  const healthyReport: AgentDependencyHealth = {
    status: 'ok',
    model: 'deepseek-v4-flash',
    checks: {
      deepseek: { ok: true, detail: 'reachable' },
      todoist: { ok: true, detail: '5 project(s)' },
    },
  };

  it('reports healthy when the agent and all dependencies are reachable', async () => {
    const activity = new BotActivityService();
    activity.recordActivity('message_text');

    const agentHealth = jest.fn().mockResolvedValue(healthyReport);
    const service = new BotStatusService(activity, { agentHealth });

    const status = await service.getFormattedStatus(123);

    expect(agentHealth).toHaveBeenCalledWith(123);
    expect(status).toContain('healthy');
    expect(status).toContain('deepseek-v4-flash');
    expect(status).toContain('Agent API: reachable');
    expect(status).toContain('DeepSeek: reachable');
    expect(status).toContain('Todoist: reachable (5 project(s))');
    expect(status).toContain('Interactions: 1');
  });

  it('reports degraded when a single dependency check fails', async () => {
    const activity = new BotActivityService();
    const agentHealth = jest.fn().mockResolvedValue({
      status: 'degraded',
      model: 'deepseek-v4-flash',
      checks: {
        deepseek: { ok: true, detail: 'reachable' },
        todoist: { ok: false, detail: 'HTTP 401' },
      },
    } as AgentDependencyHealth);
    const service = new BotStatusService(activity, { agentHealth });

    const status = await service.getFormattedStatus(123);

    expect(status).toContain('degraded');
    expect(status).toContain('Todoist: degraded (HTTP 401)');
    expect(status).toContain('DeepSeek: reachable');
  });

  it('reports degraded and unreachable when the agent probe throws', async () => {
    const activity = new BotActivityService();
    const agentHealth = jest.fn().mockRejectedValue(new Error('connect ECONNREFUSED'));
    const service = new BotStatusService(activity, { agentHealth });

    const status = await service.getFormattedStatus(123);

    expect(status).toContain('degraded');
    expect(status).toContain('Agent API: unreachable (connect ECONNREFUSED)');
    // Falls back to the configured/default model name when the agent can't report it.
    expect(status).toContain('deepseek-v4-flash');
  });

  it('reports degraded when no agent health probe is configured', async () => {
    const activity = new BotActivityService();
    const service = new BotStatusService(activity, { agentModel: 'deepseek-v4-flash' });

    const status = await service.getFormattedStatus();

    expect(status).toContain('degraded');
    expect(status).toContain('Agent API: unreachable (not configured)');
  });
});

// src/services/telegram/bot-status.service.ts — Aggregates system health information
// for the /status command. Reports runtime uptime, interaction metrics, and — via the
// Python agent's deep-health probe — the live provider/model plus downstream
// dependency reachability. Outputs a Markdown-formatted status card.
//
// LLM and Todoist execution live entirely in the Python agent, so their health can
// only be checked there (Todoist tokens are resolved per Telegram user). This service
// renders whatever the agent reports; if it is unreachable, the card says so honestly.

import { BotActivityService } from './bot-activity.service';

// A single downstream dependency's health, as reported by the Python probe.
export interface DependencyCheck {
  ok: boolean;
  detail: string;
}

export type AgentProvider = 'deepseek' | 'openai';

// The structured result of the agent's /health/detail deep-probe.
export interface AgentDependencyHealth {
  status: 'ok' | 'degraded';
  provider: AgentProvider;
  model: string;
  checks: Record<string, DependencyCheck>;
}

// Injected probe — typically bound to LangGraphAgentClient.fetchDependencyHealth.
export type AgentHealthProbe = (telegramUserId?: number) => Promise<AgentDependencyHealth>;

export interface BotStatusServiceOptions {
  agentProvider?: AgentProvider;
  agentModel?: string;
  agentHealth?: AgentHealthProbe;
}

export interface BotStatusSnapshot {
  runtime: {
    ok: boolean;
    uptimeMs: number;
    startedAt: Date;
  };
  agent: {
    // Whether the Python agent API answered the health probe at all.
    reachable: boolean;
    provider: AgentProvider;
    model: string;
    // Per-dependency checks (llm, todoist, etc.). Empty when the agent is unreachable.
    checks: Record<string, DependencyCheck>;
    // Populated with the error reason when the agent is unreachable.
    detail?: string;
  };
  activity: {
    totalInteractions: number;
    lastActivityAt: Date | null;
    lastActivityType: string | null;
  };
}

// Human-friendly labels for known dependency check keys.
const CHECK_LABELS: Record<string, string> = {
  deepseek: 'DeepSeek',
  openai: 'OpenAI',
  log_writer: 'Log writer',
  todoist: 'Todoist',
};

export class BotStatusService {
  private readonly fallbackProvider: AgentProvider;
  private readonly fallbackModel: string;
  private readonly agentHealth?: AgentHealthProbe;

  constructor(
    private readonly activityService: BotActivityService,
    options: BotStatusServiceOptions = {},
  ) {
    const configuredProvider = process.env.LLM_PROVIDER?.trim().toLowerCase();
    this.fallbackProvider =
      options.agentProvider || (configuredProvider === 'deepseek' ? 'deepseek' : 'openai');
    this.fallbackModel =
      options.agentModel ||
      (this.fallbackProvider === 'openai'
        ? process.env.OPENAI_MODEL || 'gpt-5.6-luna'
        : process.env.DEEPSEEK_MODEL || 'deepseek-v4-flash');
    this.agentHealth = options.agentHealth;
  }

  async getSnapshot(telegramUserId?: number): Promise<BotStatusSnapshot> {
    const activity = this.activityService.getSnapshot();
    const agent = await this.getAgentStatus(telegramUserId);

    return {
      runtime: {
        ok: true,
        uptimeMs: activity.uptimeMs,
        startedAt: activity.startedAt,
      },
      agent,
      activity: {
        totalInteractions: activity.totalInteractions,
        lastActivityAt: activity.lastActivityAt,
        lastActivityType: activity.lastActivityType,
      },
    };
  }

  // Builds the human-readable status card shown to the user via the /status command.
  async getFormattedStatus(telegramUserId?: number): Promise<string> {
    const snapshot = await this.getSnapshot(telegramUserId);
    const overallStatus = this.isHealthy(snapshot) ? 'healthy' : 'degraded';

    const statusEmoji = overallStatus === 'healthy' ? '✅' : '⚠️';

    const lines = [
      `### ${statusEmoji} Jarvis — ${overallStatus}`,
      '',
      '---',
      '',
      `### 🖥️ Runtime`,
      '',
      `* Status: ${snapshot.runtime.ok ? 'online' : 'offline'}`,
      `* Uptime: ${this.formatDuration(snapshot.runtime.uptimeMs)}`,
      '',
      `### 🧠 AI`,
      '',
      `* Provider: ${this.providerLabel(snapshot.agent.provider)}`,
      `* Model: \`${snapshot.agent.model}\``,
      `* Agent API: ${snapshot.agent.reachable ? 'reachable' : `unreachable (${snapshot.agent.detail})`}`,
      '',
      `### 🔌 Dependencies`,
      '',
      ...this.formatDependencyLines(snapshot.agent),
      '',
      `### 📊 Activity`,
      '',
      `* Interactions: ${snapshot.activity.totalInteractions}`,
      `* Last: ${this.formatLastActivity(snapshot.activity.lastActivityAt)} (${snapshot.activity.lastActivityType || 'none'})`,
    ];

    return lines.join('\n');
  }

  // Overall health is only "healthy" when the runtime is up, the agent answered,
  // and every dependency it reported is ok.
  private isHealthy(snapshot: BotStatusSnapshot): boolean {
    if (!snapshot.runtime.ok || !snapshot.agent.reachable) return false;
    return Object.values(snapshot.agent.checks).every((check) => check.ok);
  }

  private formatDependencyLines(agent: BotStatusSnapshot['agent']): string[] {
    if (!agent.reachable) {
      return ['* Unknown — agent API unreachable'];
    }

    const keys = Object.keys(agent.checks);
    if (keys.length === 0) {
      return ['* None reported'];
    }

    return keys.map((key) => {
      const check = agent.checks[key];
      const label = key === 'llm' ? this.providerLabel(agent.provider) : CHECK_LABELS[key] || key;
      const state = check.ok ? 'reachable' : 'degraded';
      return `* ${label}: ${state} (${check.detail})`;
    });
  }

  private async getAgentStatus(telegramUserId?: number): Promise<BotStatusSnapshot['agent']> {
    if (!this.agentHealth) {
      return {
        reachable: false,
        provider: this.fallbackProvider,
        model: this.fallbackModel,
        checks: {},
        detail: 'not configured',
      };
    }

    try {
      const health = await this.agentHealth(telegramUserId);
      return {
        reachable: true,
        provider: health.provider || this.fallbackProvider,
        model: health.model || this.fallbackModel,
        checks: health.checks || {},
      };
    } catch (error) {
      return {
        reachable: false,
        provider: this.fallbackProvider,
        model: this.fallbackModel,
        checks: {},
        detail: (error as Error).message,
      };
    }
  }

  private providerLabel(provider: AgentProvider): string {
    return provider === 'openai' ? 'OpenAI' : 'DeepSeek';
  }

  private formatDuration(durationMs: number): string {
    const totalSeconds = Math.max(0, Math.floor(durationMs / 1000));
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    return `${hours}h ${minutes}m ${seconds}s`;
  }

  private formatLastActivity(lastActivityAt: Date | null): string {
    if (!lastActivityAt) return 'none yet';

    const elapsedMs = Date.now() - lastActivityAt.getTime();
    const elapsedSeconds = Math.max(0, Math.floor(elapsedMs / 1000));

    if (elapsedSeconds < 60) return `${elapsedSeconds}s ago`;

    const elapsedMinutes = Math.floor(elapsedSeconds / 60);
    if (elapsedMinutes < 60) return `${elapsedMinutes}m ago`;

    return `${Math.floor(elapsedMinutes / 60)}h ago`;
  }
}

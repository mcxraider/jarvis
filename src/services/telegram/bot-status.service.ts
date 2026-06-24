// src/services/telegram/bot-status.service.ts — Aggregates system health information
// for the /status command. Checks runtime uptime, the configured AI model, Todoist API
// reachability, and interaction metrics. Outputs a Markdown-formatted status card.

import { BotActivityService } from './bot-activity.service';

export interface TodoistHealthCheck {
  getProjects: () => Promise<{ results: { id: string }[]; next_cursor: string | null }>;
}

export interface BotStatusServiceOptions {
  agentModel?: string;
  todoistService?: TodoistHealthCheck;
}

export interface BotStatusSnapshot {
  runtime: {
    ok: boolean;
    uptimeMs: number;
    startedAt: Date;
  };
  agent: {
    model: string;
  };
  todoist: {
    ok: boolean;
    detail: string;
  };
  activity: {
    totalInteractions: number;
    lastActivityAt: Date | null;
    lastActivityType: string | null;
  };
}

export class BotStatusService {
  private readonly agentModel: string;
  private readonly todoistService?: TodoistHealthCheck;

  constructor(
    private readonly activityService: BotActivityService,
    options: BotStatusServiceOptions = {},
  ) {
    this.agentModel = options.agentModel || process.env.DEEPSEEK_MODEL || 'deepseek-v4-flash';
    this.todoistService = options.todoistService;
  }

  async getSnapshot(): Promise<BotStatusSnapshot> {
    const activity = this.activityService.getSnapshot();
    const todoist = await this.getTodoistStatus();

    return {
      runtime: {
        ok: true,
        uptimeMs: activity.uptimeMs,
        startedAt: activity.startedAt,
      },
      agent: {
        model: this.agentModel,
      },
      todoist,
      activity: {
        totalInteractions: activity.totalInteractions,
        lastActivityAt: activity.lastActivityAt,
        lastActivityType: activity.lastActivityType,
      },
    };
  }

  // Builds the human-readable status card shown to the user via the /status command.
  async getFormattedStatus(): Promise<string> {
    const snapshot = await this.getSnapshot();
    const overallStatus = snapshot.todoist.ok ? 'healthy' : 'degraded';

    return [
      `**Jarvis — ${overallStatus}**`,
      '',
      `**Runtime**`,
      `• Status: ${snapshot.runtime.ok ? 'online' : 'offline'}`,
      `• Uptime: ${this.formatDuration(snapshot.runtime.uptimeMs)}`,
      '',
      `**AI**`,
      `• Model: \`${snapshot.agent.model}\``,
      '',
      `**Dependencies**`,
      `• Todoist: ${snapshot.todoist.ok ? 'reachable' : 'degraded'} (${snapshot.todoist.detail})`,
      '',
      `**Activity**`,
      `• Interactions: ${snapshot.activity.totalInteractions}`,
      `• Last: ${this.formatLastActivity(snapshot.activity.lastActivityAt)} (${snapshot.activity.lastActivityType || 'none'})`,
    ].join('\n');
  }

  private async getTodoistStatus(): Promise<BotStatusSnapshot['todoist']> {
    if (!this.todoistService) {
      return { ok: false, detail: 'not configured' };
    }

    try {
      const projects = await this.todoistService.getProjects();
      return { ok: true, detail: `${projects.results.length} project(s) visible` };
    } catch (error) {
      return { ok: false, detail: (error as Error).message };
    }
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

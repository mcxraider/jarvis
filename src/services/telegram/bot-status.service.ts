import { GPT_CONSTANTS } from '../ai/constants/gpt.constants';
import { TodoistAPIService } from '../external/todoist-api.service';
import { BotActivityService } from './bot-activity.service';

export interface BotStatusServiceOptions {
  gptModel?: string;
  todoistService?: Pick<TodoistAPIService, 'getProjects'>;
}

export interface BotStatusSnapshot {
  runtime: {
    ok: boolean;
    uptimeMs: number;
    startedAt: Date;
  };
  gpt: {
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

/**
 * Aggregates runtime health for the Telegram /status command.
 */
export class BotStatusService {
  private readonly gptModel: string;
  private readonly todoistService?: Pick<TodoistAPIService, 'getProjects'>;

  constructor(
    private readonly activityService: BotActivityService,
    options: BotStatusServiceOptions = {},
  ) {
    this.gptModel = options.gptModel || GPT_CONSTANTS.DEFAULT_MODEL;
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
      gpt: {
        model: this.gptModel,
      },
      todoist,
      activity: {
        totalInteractions: activity.totalInteractions,
        lastActivityAt: activity.lastActivityAt,
        lastActivityType: activity.lastActivityType,
      },
    };
  }

  async getFormattedStatus(): Promise<string> {
    const snapshot = await this.getSnapshot();
    const overallStatus = snapshot.todoist.ok ? 'healthy' : 'degraded';

    return [
      `🤖 *Jarvis Status: ${overallStatus.toUpperCase()}*`,
      '',
      `*Runtime*`,
      `• Bot: ${snapshot.runtime.ok ? 'online' : 'offline'}`,
      `• Uptime: ${this.formatDuration(snapshot.runtime.uptimeMs)}`,
      `• Started: ${snapshot.runtime.startedAt.toISOString()}`,
      '',
      `*AI*`,
      `• GPT model: ${snapshot.gpt.model}`,
      '',
      `*Dependencies*`,
      `• Todoist: ${snapshot.todoist.ok ? 'reachable' : 'degraded'} (${snapshot.todoist.detail})`,
      '',
      `*Recent Activity*`,
      `• Total interactions: ${snapshot.activity.totalInteractions}`,
      `• Last activity: ${this.formatLastActivity(snapshot.activity.lastActivityAt)}`,
      `• Last activity type: ${snapshot.activity.lastActivityType || 'none yet'}`,
    ].join('\n');
  }

  private async getTodoistStatus(): Promise<BotStatusSnapshot['todoist']> {
    if (!this.todoistService) {
      return {
        ok: false,
        detail: 'not configured',
      };
    }

    try {
      const projects = await this.todoistService.getProjects();
      return {
        ok: true,
        detail: `${projects.length} project(s) visible`,
      };
    } catch (error) {
      return {
        ok: false,
        detail: (error as Error).message,
      };
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
    if (!lastActivityAt) {
      return 'none yet';
    }

    const elapsedMs = Date.now() - lastActivityAt.getTime();
    const elapsedSeconds = Math.max(0, Math.floor(elapsedMs / 1000));

    if (elapsedSeconds < 60) {
      return `${elapsedSeconds}s ago`;
    }

    const elapsedMinutes = Math.floor(elapsedSeconds / 60);
    if (elapsedMinutes < 60) {
      return `${elapsedMinutes}m ago`;
    }

    const elapsedHours = Math.floor(elapsedMinutes / 60);
    return `${elapsedHours}h ago`;
  }
}

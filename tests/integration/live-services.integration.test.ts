import dotenv from 'dotenv';
import { MessageProcessorService } from '../../src/services/telegram/message-processor.service';
import { LangGraphAgentClient } from '../../src/services/ai/langgraph-agent-client.service';
import { TextProcessorService } from '../../src/services/telegram/processors/text-processor.service';
import { AudioProcessorService } from '../../src/services/telegram/processors/audio-processor.service';
import { MemoryPendingClarificationStore } from '../../src/services/telegram/pending-clarification.store';
import { MemoryConversationGateStore } from '../../src/services/telegram/conversation-gate.store';
import { createTestRunLogger } from '../helpers/test-run-logger';

dotenv.config();

const logger = createTestRunLogger('integration-live-services');
const TEST_LABEL = 'jarvis-test';
const TODOIST_API_BASE_URL = 'https://api.todoist.com/api/v1';

interface TodoistTask {
  id: string;
  content: string;
  priority: number;
}

interface TodoistTaskPage {
  results: TodoistTask[];
}

class TodoistLiveTestClient {
  constructor(private readonly apiKey: string) {}

  addTask(payload: { content: string; labels?: string[]; priority?: number }): Promise<TodoistTask> {
    return this.request('/tasks', { method: 'POST', body: JSON.stringify(payload) });
  }

  getTask(taskId: string): Promise<TodoistTask> {
    return this.request(`/tasks/${taskId}`);
  }

  getTasks(options: { label: string }): Promise<TodoistTaskPage> {
    const query = new URLSearchParams({ label: options.label });
    return this.request(`/tasks?${query.toString()}`);
  }

  updateTask(
    taskId: string,
    payload: { content?: string; priority?: number },
  ): Promise<TodoistTask> {
    return this.request(`/tasks/${taskId}`, { method: 'POST', body: JSON.stringify(payload) });
  }

  async completeTask(taskId: string): Promise<void> {
    await this.request(`/tasks/${taskId}/close`, { method: 'POST' });
  }

  async deleteTask(taskId: string): Promise<void> {
    await this.request(`/tasks/${taskId}`, { method: 'DELETE' });
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(`${TODOIST_API_BASE_URL}${path}`, {
      ...init,
      headers: {
        Authorization: `Bearer ${this.apiKey}`,
        'Content-Type': 'application/json',
        ...init.headers,
      },
    });
    if (!response.ok) {
      throw new Error(`Todoist API error (${response.status}): ${await response.text()}`);
    }
    if (response.status === 204) return undefined as T;
    const body = await response.text();
    return (body ? JSON.parse(body) : undefined) as T;
  }
}

function uniqueName(prefix: string): string {
  return `${prefix}-${Date.now()}`;
}

function describeIf(condition: boolean): jest.Describe {
  return condition ? describe : describe.skip;
}

async function findTaskByContent(
  todoist: TodoistLiveTestClient,
  content: string,
): Promise<TodoistTask | undefined> {
  const page = await todoist.getTasks({ label: TEST_LABEL });
  return page.results.find((task) => task.content.includes(content));
}

async function cleanupTask(todoist: TodoistLiveTestClient, taskId: string | undefined): Promise<void> {
  if (!taskId) return;

  try {
    await todoist.deleteTask(taskId);
    logger.logStep('cleanup_deleted_task', { taskId });
  } catch (deleteError) {
    logger.logStep('cleanup_delete_failed_attempting_complete', {
      taskId,
      error: (deleteError as Error).message,
    });

    try {
      await todoist.completeTask(taskId);
      logger.logStep('cleanup_completed_task', { taskId });
    } catch (completeError) {
      logger.logStep('cleanup_failed', {
        taskId,
        error: (completeError as Error).message,
      });
    }
  }
}

afterAll(() => {
  logger.writeSummary();
});

describeIf(process.env.RUN_LIVE_TODOIST_TESTS === 'true' && !!process.env.TODOIST_API_KEY)(
  'Live Todoist CRUD',
  () => {
    let todoist: TodoistLiveTestClient;

    beforeAll(() => {
      todoist = new TodoistLiveTestClient(process.env.TODOIST_API_KEY!);
    });

    it(
      'creates, fetches, updates, lists, completes, and deletes real Todoist tasks',
      async () => {
        const taskName = uniqueName('jarvis-live-test');
        const cleanupName = uniqueName('jarvis-delete-test');
        let taskId: string | undefined;
        let cleanupTaskId: string | undefined;

        try {
          const created = await todoist.addTask({
            content: taskName,
            labels: [TEST_LABEL],
            priority: 1,
          });
          taskId = created.id;
          logger.logResponse('todoist_create_task', { created });

          const fetched = await todoist.getTask(created.id);
          expect(fetched.content).toBe(taskName);

          const updated = await todoist.updateTask(created.id, {
            content: `${taskName}-updated`,
            priority: 4,
          });
          logger.logResponse('todoist_update_task', { updated });
          expect(updated.content).toBe(`${taskName}-updated`);
          expect(updated.priority).toBe(4);

          const listed = await todoist.getTasks({ label: TEST_LABEL });
          logger.logResponse('todoist_list_tasks', {
            count: listed.results.length,
            ids: listed.results.map((task) => task.id),
          });
          expect(listed.results.some((task) => task.id === created.id)).toBe(true);

          await expect(todoist.completeTask(created.id)).resolves.toBeUndefined();
          logger.logStep('todoist_complete_task', { taskId: created.id });
          taskId = undefined;

          const deleteCandidate = await todoist.addTask({
            content: cleanupName,
            labels: [TEST_LABEL],
          });
          cleanupTaskId = deleteCandidate.id;
          await expect(todoist.deleteTask(deleteCandidate.id)).resolves.toBeUndefined();
          logger.logStep('todoist_delete_task', { taskId: deleteCandidate.id });
          cleanupTaskId = undefined;
        } finally {
          await cleanupTask(todoist, taskId);
          await cleanupTask(todoist, cleanupTaskId);
        }
      },
      30000,
    );
  },
);

describeIf(
  process.env.RUN_LIVE_LANGGRAPH_TODOIST_TESTS === 'true' &&
    !!process.env.LANGGRAPH_AGENT_URL &&
    !!process.env.TODOIST_API_KEY,
)('Live LangGraph + Todoist pipeline', () => {
  let todoist: TodoistLiveTestClient;

  beforeAll(() => {
    todoist = new TodoistLiveTestClient(process.env.TODOIST_API_KEY!);
  });

  it(
    'creates a Todoist task through MessageProcessorService and LangGraph tool calling',
    async () => {
      const taskName = uniqueName('jarvis-langgraph-test');
      let createdTaskId: string | undefined;

      try {
        const gateStore = new MemoryConversationGateStore();
        const pendingStore = new MemoryPendingClarificationStore();
        const textProcessor = new TextProcessorService(
          new LangGraphAgentClient(),
          pendingStore,
          gateStore,
        );
        const messageProcessor = new MessageProcessorService(
          textProcessor,
          new AudioProcessorService({} as any, textProcessor),
          gateStore,
          pendingStore,
        );
        const prompt = `Add a Todoist task named ${taskName} with label ${TEST_LABEL} and normal priority.`;
        const response = await messageProcessor.processTextMessage(prompt, 123456);
        logger.logResponse('langgraph_todoist_pipeline', { prompt, response });

        expect(response.response.length).toBeGreaterThan(0);

        const created = await findTaskByContent(todoist, taskName);
        logger.logAssertion('langgraph_task_lookup', { created });
        expect(created).toBeDefined();
        createdTaskId = created?.id;
      } finally {
        await cleanupTask(todoist, createdTaskId);
      }
    },
    60000,
  );
});

describeIf(
  process.env.RUN_LIVE_WEBHOOK_TESTS === 'true' &&
    !!process.env.NGROK_URL &&
    !!process.env.TELEGRAM_SECRET_TOKEN &&
    !!process.env.TODOIST_API_KEY &&
    !!process.env.TEST_CHAT_ID &&
    !!process.env.TEST_USER_ID,
)('Live webhook pipeline', () => {
  let todoist: TodoistLiveTestClient;

  beforeAll(() => {
    todoist = new TodoistLiveTestClient(process.env.TODOIST_API_KEY!);
  });

  it(
    'posts a Telegram-shaped update to the live webhook and verifies Todoist side effect',
    async () => {
      const taskName = uniqueName('jarvis-webhook-test');
      let createdTaskId: string | undefined;

      try {
        const webhookUrl = `${process.env.NGROK_URL}/webhook/${process.env.TELEGRAM_SECRET_TOKEN}`;
        const update = {
          update_id: Date.now(),
          message: {
            message_id: Date.now(),
            date: Math.floor(Date.now() / 1000),
            chat: { id: Number(process.env.TEST_CHAT_ID), type: 'private' },
            from: {
              id: Number(process.env.TEST_USER_ID),
              is_bot: false,
              first_name: 'Jarvis Test',
            },
            text: `Add a Todoist task named ${taskName} with label ${TEST_LABEL}.`,
          },
        };

        const response = await fetch(webhookUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(update),
        });
        logger.logRequest('live_webhook_update', { webhookUrl, update });
        logger.logResponse('live_webhook_update', { status: response.status });

        expect(response.status).toBe(200);

        const created = await findTaskByContent(todoist, taskName);
        logger.logAssertion('webhook_task_lookup', { created });
        expect(created).toBeDefined();
        createdTaskId = created?.id;
      } finally {
        await cleanupTask(todoist, createdTaskId);
      }
    },
    60000,
  );
});

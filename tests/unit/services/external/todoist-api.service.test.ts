import { TodoistAPIService } from '../../../../src/services/external/todoist-api.service';
import { createTestRunLogger } from '../../../helpers/test-run-logger';

const logger = createTestRunLogger('unit-todoist-api-service');

function createResponse(options: {
  ok?: boolean;
  status?: number;
  body?: unknown;
  text?: string;
  contentLength?: string | null;
}): Response {
  const body = options.body ?? {};

  return {
    ok: options.ok ?? true,
    status: options.status ?? 200,
    headers: {
      get: jest.fn((name: string) => {
        if (name.toLowerCase() === 'content-length') {
          return options.contentLength ?? null;
        }
        return null;
      }),
    },
    json: jest.fn().mockResolvedValue(body),
    text: jest.fn().mockResolvedValue(options.text ?? JSON.stringify(body)),
  } as unknown as Response;
}

describe('TodoistAPIService', () => {
  const originalFetch = global.fetch;
  let fetchMock: jest.Mock;
  let service: TodoistAPIService;

  beforeEach(() => {
    fetchMock = jest.fn();
    global.fetch = fetchMock;
    service = new TodoistAPIService('todoist-test-key');
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  afterAll(() => {
    logger.writeSummary();
  });

  it('serializes addTask as POST /tasks with JSON body', async () => {
    const task = { id: 'task-1', content: 'Review invoices' };
    fetchMock.mockResolvedValue(createResponse({ body: task }));

    await expect(
      service.addTask({
        content: 'Review invoices',
        due_string: 'tomorrow at 9am',
        priority: 4,
        labels: ['jarvis-test'],
      }),
    ).resolves.toEqual(task);

    const [url, options] = fetchMock.mock.calls[0];
    logger.logRequest('addTask', { url, options });
    expect(url).toBe('https://api.todoist.com/api/v1/tasks');
    expect(options).toMatchObject({
      method: 'POST',
      headers: {
        Authorization: 'Bearer todoist-test-key',
        'Content-Type': 'application/json',
      },
    });
    expect(JSON.parse(options.body)).toEqual({
      content: 'Review invoices',
      due_string: 'tomorrow at 9am',
      priority: 4,
      labels: ['jarvis-test'],
    });
  });

  it('serializes getTask as GET /tasks/:id', async () => {
    fetchMock.mockResolvedValue(createResponse({ body: { id: 'task-1' } }));

    await service.getTask('task-1');

    logger.logRequest('getTask', { call: fetchMock.mock.calls[0] });
    expect(fetchMock).toHaveBeenCalledWith('https://api.todoist.com/api/v1/tasks/task-1', {
      method: 'GET',
      headers: {
        Authorization: 'Bearer todoist-test-key',
        'Content-Type': 'application/json',
      },
    });
  });

  it('serializes getTasks query parameters', async () => {
    fetchMock.mockResolvedValue(createResponse({ body: [] }));

    await service.getTasks({
      project_id: 'project-1',
      section_id: 'section-1',
      label: 'jarvis-test',
      filter: 'today',
      lang: 'en',
      ids: ['1', '2'],
    });

    const [url] = fetchMock.mock.calls[0];
    logger.logRequest('getTasks', { url });
    expect(url).toBe(
      'https://api.todoist.com/api/v1/tasks?project_id=project-1&section_id=section-1&label=jarvis-test&filter=today&lang=en&ids=1%2C2',
    );
  });

  it('serializes updateTask as POST /tasks/:id with JSON body', async () => {
    fetchMock.mockResolvedValue(createResponse({ body: { id: 'task-1' } }));

    await service.updateTask('task-1', {
      content: 'Review invoices now',
      priority: 4,
    });

    const [url, options] = fetchMock.mock.calls[0];
    logger.logRequest('updateTask', { url, options });
    expect(url).toBe('https://api.todoist.com/api/v1/tasks/task-1');
    expect(options.method).toBe('POST');
    expect(JSON.parse(options.body)).toEqual({
      content: 'Review invoices now',
      priority: 4,
    });
  });

  it('serializes completeTask as POST /tasks/:id/close', async () => {
    fetchMock.mockResolvedValue(createResponse({ status: 204, contentLength: '0' }));

    await expect(service.completeTask('task-1')).resolves.toBeUndefined();

    logger.logRequest('completeTask', { call: fetchMock.mock.calls[0] });
    expect(fetchMock).toHaveBeenCalledWith(
      'https://api.todoist.com/api/v1/tasks/task-1/close',
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('serializes deleteTask as DELETE /tasks/:id', async () => {
    fetchMock.mockResolvedValue(createResponse({ status: 204, contentLength: '0' }));

    await expect(service.deleteTask('task-1')).resolves.toBeUndefined();

    logger.logRequest('deleteTask', { call: fetchMock.mock.calls[0] });
    expect(fetchMock).toHaveBeenCalledWith(
      'https://api.todoist.com/api/v1/tasks/task-1',
      expect.objectContaining({ method: 'DELETE' }),
    );
  });

  it('serializes getProjects and health checks', async () => {
    fetchMock.mockResolvedValue(createResponse({ body: [{ id: 'project-1' }] }));

    await expect(service.getProjects()).resolves.toEqual([{ id: 'project-1' }]);
    await expect(service.checkHealth()).resolves.toEqual({
      ok: true,
      detail: '1 project(s) visible',
    });

    logger.logRequest('getProjects', { calls: fetchMock.mock.calls });
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'https://api.todoist.com/api/v1/projects',
      expect.objectContaining({ method: 'GET' }),
    );
  });

  it('returns degraded health when Todoist projects fail', async () => {
    fetchMock.mockResolvedValue(
      createResponse({ ok: false, status: 401, text: 'invalid token' }),
    );

    await expect(service.checkHealth()).resolves.toEqual({
      ok: false,
      detail: 'Todoist API error (401): invalid token',
    });
  });

  it('serializes completed task requests through the Sync API', async () => {
    fetchMock.mockResolvedValue(createResponse({ body: { items: [{ id: 'done-1' }] } }));

    await expect(
      service.getCompletedTasks({
        since: '2026-06-01T00:00:00Z',
        until: '2026-06-16T23:59:59Z',
        project_id: 'project-1',
        limit: 25,
        offset: 5,
      }),
    ).resolves.toEqual([{ id: 'done-1' }]);

    const [url, options] = fetchMock.mock.calls[0];
    logger.logRequest('getCompletedTasks', { url, options });
    expect(url).toBe(
      'https://api.todoist.com/sync/v9/completed/get_all?since=2026-06-01T00%3A00%3A00Z&until=2026-06-16T23%3A59%3A59Z&project_id=project-1&limit=25&offset=5',
    );
    expect(options).toEqual({
      method: 'GET',
      headers: {
        Authorization: 'Bearer todoist-test-key',
      },
    });
  });

  it('throws useful errors for REST and Sync API failures', async () => {
    fetchMock.mockResolvedValueOnce(
      createResponse({ ok: false, status: 500, text: 'rest exploded' }),
    );
    await expect(service.getTask('task-1')).rejects.toThrow(
      'Todoist API error (500): rest exploded',
    );

    fetchMock.mockResolvedValueOnce(
      createResponse({ ok: false, status: 403, text: 'sync denied' }),
    );
    await expect(service.getCompletedTasks()).rejects.toThrow(
      'Todoist Sync API error (403): sync denied',
    );

    logger.logResponse('apiFailures', {
      restError: 'Todoist API error (500): rest exploded',
      syncError: 'Todoist Sync API error (403): sync denied',
    });
  });
});

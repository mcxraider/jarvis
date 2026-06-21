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
    jest.useRealTimers();
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
    fetchMock.mockResolvedValue(createResponse({ body: { results: [], next_cursor: null } }));

    await expect(service.getTasks({
      project_id: 'project-1',
      section_id: 'section-1',
      parent_id: 'parent-1',
      label: 'jarvis-test',
      ids: ['1', '2'],
      goal_id: '550e8400-e29b-41d4-a716-446655440000',
      cursor: 'page-2',
      limit: 25,
    })).resolves.toEqual({ results: [], next_cursor: null });

    const [url] = fetchMock.mock.calls[0];
    logger.logRequest('getTasks', { url });
    expect(url).toBe(
      'https://api.todoist.com/api/v1/tasks?project_id=project-1&section_id=section-1&parent_id=parent-1&label=jarvis-test&ids=1%2C2&goal_id=550e8400-e29b-41d4-a716-446655440000&cursor=page-2&limit=25',
    );
  });

  it('serializes getTasksByFilter through the dedicated filter endpoint', async () => {
    fetchMock.mockResolvedValue(
      createResponse({ body: { results: [{ id: 'task-1' }], next_cursor: 'next-page' } }),
    );

    await expect(service.getTasksByFilter({
      query: 'Jun 25 & p1',
      lang: 'en',
      cursor: 'page-2',
      limit: 10,
    })).resolves.toEqual({ results: [{ id: 'task-1' }], next_cursor: 'next-page' });

    expect(fetchMock.mock.calls[0][0]).toBe(
      'https://api.todoist.com/api/v1/tasks/filter?query=Jun+25+%26+p1&lang=en&cursor=page-2&limit=10',
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

  it('accepts successful close and delete responses with empty 200 bodies', async () => {
    fetchMock
      .mockResolvedValueOnce(createResponse({ status: 200, text: '' }))
      .mockResolvedValueOnce(createResponse({ status: 200, text: '' }));

    await expect(service.completeTask('task-1')).resolves.toBeUndefined();
    await expect(service.deleteTask('task-2')).resolves.toBeUndefined();
  });

  it('serializes getProjects and health checks', async () => {
    fetchMock.mockResolvedValue(
      createResponse({ body: { results: [{ id: 'project-1' }], next_cursor: null } }),
    );

    await expect(service.getProjects()).resolves.toEqual({
      results: [{ id: 'project-1' }],
      next_cursor: null,
    });
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

  it('serializes completed task requests through Todoist API v1', async () => {
    fetchMock.mockResolvedValue(
      createResponse({ body: { items: [{ id: 'done-1' }], next_cursor: 'next-page' } }),
    );

    await expect(
      service.getCompletedTasks({
        since: '2026-06-01T00:00:00Z',
        until: '2026-06-16T23:59:59Z',
        workspace_id: 123456,
        project_id: 'project-1',
        section_id: 'section-1',
        parent_id: 'parent-1',
        filter_query: '@work',
        filter_lang: 'en',
        cursor: 'page-2',
        limit: 25,
      }),
    ).resolves.toEqual({ items: [{ id: 'done-1' }], next_cursor: 'next-page' });

    const [url, options] = fetchMock.mock.calls[0];
    logger.logRequest('getCompletedTasks', { url, options });
    expect(url).toBe(
      'https://api.todoist.com/api/v1/tasks/completed/by_completion_date?since=2026-06-01T00%3A00%3A00Z&until=2026-06-16T23%3A59%3A59Z&workspace_id=123456&project_id=project-1&section_id=section-1&parent_id=parent-1&filter_query=%40work&filter_lang=en&cursor=page-2&limit=25',
    );
    expect(options).toMatchObject({
      method: 'GET',
      headers: {
        Authorization: 'Bearer todoist-test-key',
        'Content-Type': 'application/json',
      },
    });
  });

  it('defaults completed task requests to the last 30 days', async () => {
    jest.useFakeTimers().setSystemTime(new Date('2026-06-19T12:00:00.000Z'));
    fetchMock.mockResolvedValue(createResponse({ body: { items: [] } }));

    await expect(service.getCompletedTasks()).resolves.toEqual({ items: [], next_cursor: null });

    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe(
      'https://api.todoist.com/api/v1/tasks/completed/by_completion_date?since=2026-05-20T12%3A00%3A00.000Z&until=2026-06-19T12%3A00%3A00.000Z',
    );
  });

  it('rejects invalid completed-task date ranges before making a request', async () => {
    await expect(service.getCompletedTasks({
      since: '2026-06-20T00:00:00Z',
      until: '2026-06-19T00:00:00Z',
    })).rejects.toThrow('until must be later than since');
    await expect(service.getCompletedTasks({
      since: '2026-01-01T00:00:00Z',
      until: '2026-05-01T00:00:00Z',
    })).rejects.toThrow('cannot exceed three months');
    await expect(service.getCompletedTasks({
      since: '2026-01-01',
      until: '2026-02-01T00:00:00Z',
    })).rejects.toThrow('RFC3339');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('throws useful errors for REST and completed-task API failures', async () => {
    fetchMock.mockResolvedValueOnce(
      createResponse({ ok: false, status: 500, text: 'rest exploded' }),
    );
    await expect(service.getTask('task-1')).rejects.toThrow(
      'Todoist API error (500): rest exploded',
    );

    fetchMock.mockResolvedValueOnce(
      createResponse({ ok: false, status: 403, text: 'completed denied' }),
    );
    await expect(service.getCompletedTasks()).rejects.toThrow(
      'Todoist API error (403): completed denied',
    );

    logger.logResponse('apiFailures', {
      restError: 'Todoist API error (500): rest exploded',
      completedTasksError: 'Todoist API error (403): completed denied',
    });
  });
});

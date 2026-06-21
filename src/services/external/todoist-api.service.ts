/**
 * Service for direct integration with Todoist REST API
 * Handles all Todoist operations without relying on MCP servers
 *
 * @module TodoistAPIService
 */

import { LogContext, logger, truncateForLog } from '../../utils/logger';

/**
 * Todoist API response interfaces
 */
export interface TodoistTask {
  user_id: string;
  id: string;
  content: string;
  description: string;
  project_id: string;
  section_id: string | null;
  parent_id: string | null;
  added_by_uid: string;
  assigned_by_uid: string | null;
  responsible_uid: string | null;
  deadline: { date: string; lang: string } | null;
  duration: { amount: number; unit: 'minute' | 'day' } | null;
  is_collapsed: boolean;
  checked: boolean;
  is_deleted: boolean;
  added_at: string;
  completed_at: string | null;
  completed_by_uid: string | null;
  updated_at: string;
  priority: number;
  labels: string[];
  due: {
    date: string;
    string: string;
    lang: string;
    is_recurring: boolean;
    timezone?: string | null;
  } | null;
  child_order: number;
  note_count: number;
  day_order: number;
  goal_ids: string[];
  completed_count: number;
  postponed_count: number;
}

export interface TodoistProject {
  id: string;
  name: string;
  description?: string;
  workspace_id?: string | null;
  parent_id?: string | null;
  child_order?: number;
  color: string;
  is_collapsed?: boolean;
  is_archived?: boolean;
  is_deleted?: boolean;
  is_favorite: boolean;
  inbox_project?: boolean;
}

export interface TodoistPaginatedResponse<T> {
  results: T[];
  next_cursor: string | null;
}

export interface CreateTaskPayload {
  content: string;
  description?: string;
  project_id?: string;
  section_id?: string;
  parent_id?: string;
  order?: number;
  labels?: string[];
  priority?: number;
  due_string?: string;
  due_date?: string;
  due_datetime?: string;
  due_lang?: string;
  assignee_id?: number;
  duration?: number;
  duration_unit?: 'minute' | 'day';
  deadline_date?: string;
}

export interface UpdateTaskPayload {
  content?: string;
  description?: string;
  labels?: string[];
  priority?: number;
  due_string?: string;
  due_date?: string;
  due_datetime?: string;
  due_lang?: string;
  assignee_id?: number | null;
  duration?: number | null;
  duration_unit?: 'minute' | 'day' | null;
  deadline_date?: string | null;
  child_order?: number;
  is_collapsed?: boolean;
  day_order?: number;
}

export interface GetTasksOptions {
  project_id?: string;
  section_id?: string;
  parent_id?: string;
  label?: string;
  ids?: string[];
  goal_id?: string;
  cursor?: string;
  limit?: number;
}

export interface GetTasksByFilterOptions {
  query: string;
  lang?: string;
  cursor?: string;
  limit?: number;
}

export interface CompletedTasksQueryOptions {
  since?: string;
  until?: string;
  workspace_id?: number;
  project_id?: string;
  section_id?: string;
  parent_id?: string;
  filter_query?: string;
  filter_lang?: string;
  cursor?: string;
  limit?: number;
}

export interface CompletedTasksResponse {
  items: unknown[];
  next_cursor?: string | null;
}

/**
 * Service class for direct Todoist API integration
 */
export class TodoistAPIService {
  private readonly apiKey: string;
  private readonly baseURL = 'https://api.todoist.com/api/v1';
  private readonly defaultCompletedTasksRangeMs = 30 * 24 * 60 * 60 * 1000;

  constructor(apiKey: string) {
    if (!apiKey) {
      throw new Error('Todoist API key is required');
    }
    this.apiKey = apiKey;
  }

  /**
   * Makes HTTP requests to Todoist API
   *
   * @param endpoint - API endpoint path
   * @param method - HTTP method
   * @param body - Request body for POST/PUT requests
   * @returns Promise<any> - API response
   */
  private async makeRequest(
    endpoint: string,
    method: 'GET' | 'POST' | 'PUT' | 'DELETE' = 'GET',
    body?: any,
    logContext: LogContext & { operation?: string } = {},
  ): Promise<any> {
    const startedAt = Date.now();
    const url = `${this.baseURL}${endpoint}`;

    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.apiKey}`,
      'Content-Type': 'application/json',
    };

    const requestOptions: RequestInit = {
      method,
      headers,
    };

    if (body && (method === 'POST' || method === 'PUT')) {
      requestOptions.body = JSON.stringify(body);
    }

    try {
      logger.debug('todoist.api.request.started', {
        ...logContext,
        url,
        method,
        hasBody: !!body,
      });

      const response = await fetch(url, requestOptions);

      if (!response.ok) {
        const errorText = await response.text();
        logger.error('todoist.api.request.failed', {
          ...logContext,
          url,
          method,
          statusCode: response.status,
          durationMs: Date.now() - startedAt,
        });
        throw new Error(`Todoist API error (${response.status}): ${errorText}`);
      }

      if (response.status === 204 || response.headers.get('content-length') === '0') {
        logger.info('todoist.api.request.completed', {
          ...logContext,
          url,
          method,
          statusCode: response.status,
          hasData: false,
          durationMs: Date.now() - startedAt,
        });
        return null;
      }

      const responseText = await response.text();
      if (!responseText.trim()) {
        logger.info('todoist.api.request.completed', {
          ...logContext,
          url,
          method,
          statusCode: response.status,
          hasData: false,
          durationMs: Date.now() - startedAt,
        });
        return null;
      }

      const data = JSON.parse(responseText);

      logger.info('todoist.api.request.completed', {
        ...logContext,
        url,
        method,
        status: response.status,
        hasData: !!data,
        durationMs: Date.now() - startedAt,
      });

      return data;
    } catch (error) {
      logger.error('todoist.api.request.failed', {
        ...logContext,
        url,
        method,
        error: (error as Error).message,
        durationMs: Date.now() - startedAt,
      });
      throw error;
    }
  }

  /**
   * Create a new task in Todoist
   *
   * @param payload - Task creation data
   * @returns Promise<TodoistTask> - Created task
   */
  async addTask(payload: CreateTaskPayload, logContext: LogContext = {}): Promise<TodoistTask> {
    logger.info('todoist.task.create.started', {
      ...logContext,
      contentPreview: truncateForLog(payload.content),
      priority: payload.priority,
      hasDescription: !!payload.description,
      hasDue: !!(payload.due_string || payload.due_date || payload.due_datetime),
      labelCount: payload.labels?.length || 0,
    });

    const task = await this.makeRequest('/tasks', 'POST', payload, {
      ...logContext,
      operation: 'todoist.task.create',
    });

    logger.info('todoist.task.create.completed', {
      ...logContext,
      taskId: task.id,
      contentPreview: truncateForLog(task.content),
    });

    return task;
  }

  /**
   * Get a specific task by ID
   *
   * @param taskId - Task ID to retrieve
   * @returns Promise<TodoistTask> - Task details
   */
  async getTask(taskId: string, logContext: LogContext = {}): Promise<TodoistTask> {
    logger.info('todoist.task.get.started', { ...logContext, taskId });

    const task = await this.makeRequest(`/tasks/${taskId}`, 'GET', undefined, {
      ...logContext,
      operation: 'todoist.task.get',
    });

    logger.info('todoist.task.get.completed', {
      ...logContext,
      taskId: task.id,
      contentPreview: truncateForLog(task.content),
    });

    return task;
  }

  /**
   * Get tasks with optional filtering
   *
   * @param options - Filtering options
   * @returns Paginated active tasks
   */
  async getTasks(
    options: GetTasksOptions = {},
    logContext: LogContext = {},
  ): Promise<TodoistPaginatedResponse<TodoistTask>> {
    logger.info('todoist.tasks.list.started', { ...logContext, ...options });

    const params = new URLSearchParams();

    if (options.project_id) params.append('project_id', options.project_id);
    if (options.section_id) params.append('section_id', options.section_id);
    if (options.parent_id) params.append('parent_id', options.parent_id);
    if (options.label) params.append('label', options.label);
    if (options.ids && options.ids.length > 0) {
      params.append('ids', options.ids.join(','));
    }
    if (options.goal_id) params.append('goal_id', options.goal_id);
    if (options.cursor) params.append('cursor', options.cursor);
    if (options.limit) params.append('limit', options.limit.toString());

    const endpoint = `/tasks${params.toString() ? '?' + params.toString() : ''}`;
    const page = await this.makeRequest(endpoint, 'GET', undefined, {
      ...logContext,
      operation: 'todoist.tasks.list',
    });

    logger.info('todoist.tasks.list.completed', {
      ...logContext,
      count: page.results.length,
      hasNextCursor: !!page.next_cursor,
    });

    return page;
  }

  async getTasksByFilter(
    options: GetTasksByFilterOptions,
    logContext: LogContext = {},
  ): Promise<TodoistPaginatedResponse<TodoistTask>> {
    logger.info('todoist.tasks.filter.started', { ...logContext, ...options });
    const params = new URLSearchParams({ query: options.query });
    if (options.lang) params.append('lang', options.lang);
    if (options.cursor) params.append('cursor', options.cursor);
    if (options.limit) params.append('limit', options.limit.toString());

    const page = await this.makeRequest(`/tasks/filter?${params.toString()}`, 'GET', undefined, {
      ...logContext,
      operation: 'todoist.tasks.filter',
    });
    logger.info('todoist.tasks.filter.completed', {
      ...logContext,
      count: page.results.length,
      hasNextCursor: !!page.next_cursor,
    });
    return page;
  }

  /**
   * Update an existing task
   *
   * @param taskId - Task ID to update
   * @param payload - Update data
   * @returns Promise<TodoistTask> - Updated task
   */
  async updateTask(
    taskId: string,
    payload: UpdateTaskPayload,
    logContext: LogContext = {},
  ): Promise<TodoistTask> {
    logger.info('todoist.task.update.started', {
      ...logContext,
      taskId,
      hasContent: !!payload.content,
      priority: payload.priority,
      hasDue: !!(payload.due_string || payload.due_date || payload.due_datetime),
      labelCount: payload.labels?.length || 0,
    });

    const task = await this.makeRequest(`/tasks/${taskId}`, 'POST', payload, {
      ...logContext,
      operation: 'todoist.task.update',
    });

    logger.info('todoist.task.update.completed', {
      ...logContext,
      taskId: task.id,
      contentPreview: truncateForLog(task.content),
    });

    return task;
  }

  /**
   * Mark a task as completed
   *
   * @param taskId - Task ID to complete
   * @returns Promise<void>
   */
  async completeTask(taskId: string, logContext: LogContext = {}): Promise<void> {
    logger.info('todoist.task.complete.started', { ...logContext, taskId });

    await this.makeRequest(`/tasks/${taskId}/close`, 'POST', undefined, {
      ...logContext,
      operation: 'todoist.task.complete',
    });

    logger.info('todoist.task.complete.completed', { ...logContext, taskId });
  }

  /**
   * Delete a task permanently
   *
   * @param taskId - Task ID to delete
   * @returns Promise<void>
   */
  async deleteTask(taskId: string, logContext: LogContext = {}): Promise<void> {
    logger.info('todoist.task.delete.started', { ...logContext, taskId });

    await this.makeRequest(`/tasks/${taskId}`, 'DELETE', undefined, {
      ...logContext,
      operation: 'todoist.task.delete',
    });

    logger.info('todoist.task.delete.completed', { ...logContext, taskId });
  }

  /**
   * Get all projects
   *
   * @returns Paginated active projects
   */
  async getProjects(
    logContext: LogContext = {},
  ): Promise<TodoistPaginatedResponse<TodoistProject>> {
    logger.info('todoist.projects.list.started', logContext);

    const page = await this.makeRequest('/projects', 'GET', undefined, {
      ...logContext,
      operation: 'todoist.projects.list',
    });

    logger.info('todoist.projects.list.completed', {
      ...logContext,
      count: page.results.length,
      hasNextCursor: !!page.next_cursor,
    });

    return page;
  }

  async checkHealth(): Promise<{ ok: boolean; detail: string }> {
    try {
      const projects = await this.getProjects();
      return {
        ok: true,
        detail: `${projects.results.length} project(s) visible`,
      };
    } catch (error) {
      return {
        ok: false,
        detail: (error as Error).message,
      };
    }
  }

  private withDefaultCompletionDateRange(options: CompletedTasksQueryOptions): CompletedTasksQueryOptions {
    const until = options.until ?? new Date().toISOString();
    this.validateRfc3339Timestamp(until, 'until');
    const since =
      options.since ??
      new Date(new Date(until).getTime() - this.defaultCompletedTasksRangeMs).toISOString();
    this.validateRfc3339Timestamp(since, 'since');

    this.validateCompletionDateRange(since, until);
    return {
      ...options,
      since,
      until,
    };
  }

  private validateRfc3339Timestamp(value: string, field: 'since' | 'until'): void {
    const rfc3339 = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;
    if (!rfc3339.test(value)) {
      throw new Error(`Todoist completed-task ${field} must be an RFC3339 timestamp`);
    }
  }

  private validateCompletionDateRange(since: string, until: string): void {
    const sinceDate = new Date(since);
    const untilDate = new Date(until);
    if (untilDate <= sinceDate) {
      throw new Error('Todoist completed-task until must be later than since');
    }
    const maximumUntil = new Date(sinceDate);
    maximumUntil.setUTCMonth(maximumUntil.getUTCMonth() + 3);
    if (untilDate > maximumUntil) {
      throw new Error('Todoist completed-task range cannot exceed three months');
    }
  }

  /**
   * Get completed tasks by completion date using Todoist API v1.
   *
   * @param options - Query options
   * @returns Promise<CompletedTasksResponse> - Completed tasks and pagination cursor
   */
  async getCompletedTasks(
    options: CompletedTasksQueryOptions = {},
    logContext: LogContext = {},
  ): Promise<CompletedTasksResponse> {
    const optionsWithRange = this.withDefaultCompletionDateRange(options);
    logger.info('todoist.completed_tasks.list.started', { ...logContext, ...optionsWithRange });

    const params = new URLSearchParams();
    params.append('since', optionsWithRange.since as string);
    params.append('until', optionsWithRange.until as string);
    if (optionsWithRange.workspace_id) params.append('workspace_id', optionsWithRange.workspace_id.toString());
    if (optionsWithRange.project_id) params.append('project_id', optionsWithRange.project_id);
    if (optionsWithRange.section_id) params.append('section_id', optionsWithRange.section_id);
    if (optionsWithRange.parent_id) params.append('parent_id', optionsWithRange.parent_id);
    if (optionsWithRange.filter_query) params.append('filter_query', optionsWithRange.filter_query);
    if (optionsWithRange.filter_lang) params.append('filter_lang', optionsWithRange.filter_lang);
    if (optionsWithRange.cursor) params.append('cursor', optionsWithRange.cursor);
    if (optionsWithRange.limit) params.append('limit', optionsWithRange.limit.toString());

    const data = await this.makeRequest(
      `/tasks/completed/by_completion_date?${params.toString()}`,
      'GET',
      undefined,
      {
        ...logContext,
        operation: 'todoist.completed_tasks.list',
      },
    );

    const result = {
      items: data.items || [],
      next_cursor: data.next_cursor ?? null,
    };

    logger.info('todoist.completed_tasks.list.completed', {
      ...logContext,
      count: result.items.length,
      hasNextCursor: !!result.next_cursor,
    });

    return result;
  }
}

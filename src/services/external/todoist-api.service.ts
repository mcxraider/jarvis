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
  id: string;
  content: string;
  description?: string;
  project_id: string;
  section_id?: string;
  parent_id?: string;
  order: number;
  priority: number;
  labels: string[];
  due?: {
    date?: string;
    datetime?: string;
    string?: string;
    timezone?: string;
  };
  url: string;
  comment_count: number;
  assignee_id?: string;
  assigner_id?: string;
  created_at: string;
  is_completed: boolean;
}

export interface TodoistProject {
  id: string;
  name: string;
  comment_count: number;
  order: number;
  color: string;
  is_shared: boolean;
  is_favorite: boolean;
  is_inbox_project: boolean;
  is_team_inbox: boolean;
  view_style: string;
  url: string;
  parent_id?: string;
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
  assignee_id?: string;
}

export interface UpdateTaskPayload {
  content?: string;
  description?: string;
  labels?: string[];
  priority?: number;
  due_string?: string;
  due_date?: string;
  due_datetime?: string;
  assignee_id?: string;
}

export interface CompletedTasksQueryOptions {
  since?: string;
  until?: string;
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

      // Handle empty responses (like DELETE requests)
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

      const data = await response.json();

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
   * @returns Promise<TodoistTask[]> - Array of tasks
   */
  async getTasks(
    options: {
      project_id?: string;
      section_id?: string;
      label?: string;
      filter?: string;
      lang?: string;
      ids?: string[];
    } = {},
    logContext: LogContext = {},
  ): Promise<TodoistTask[]> {
    logger.info('todoist.tasks.list.started', { ...logContext, ...options });

    const params = new URLSearchParams();

    if (options.project_id) params.append('project_id', options.project_id);
    if (options.section_id) params.append('section_id', options.section_id);
    if (options.label) params.append('label', options.label);
    if (options.filter) params.append('filter', options.filter);
    if (options.lang) params.append('lang', options.lang);
    if (options.ids && options.ids.length > 0) {
      params.append('ids', options.ids.join(','));
    }

    const endpoint = `/tasks${params.toString() ? '?' + params.toString() : ''}`;
    const tasks = await this.makeRequest(endpoint, 'GET', undefined, {
      ...logContext,
      operation: 'todoist.tasks.list',
    });

    logger.info('todoist.tasks.list.completed', {
      ...logContext,
      count: tasks.length,
      filter: options.filter,
    });

    return tasks;
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
   * @returns Promise<TodoistProject[]> - Array of projects
   */
  async getProjects(logContext: LogContext = {}): Promise<TodoistProject[]> {
    logger.info('todoist.projects.list.started', logContext);

    const projects = await this.makeRequest('/projects', 'GET', undefined, {
      ...logContext,
      operation: 'todoist.projects.list',
    });

    logger.info('todoist.projects.list.completed', {
      ...logContext,
      count: projects.length,
    });

    return projects;
  }

  async checkHealth(): Promise<{ ok: boolean; detail: string }> {
    try {
      const projects = await this.getProjects();
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

  private withDefaultCompletionDateRange(options: CompletedTasksQueryOptions): CompletedTasksQueryOptions {
    const until = options.until ?? new Date().toISOString();
    const since =
      options.since ??
      new Date(new Date(until).getTime() - this.defaultCompletedTasksRangeMs).toISOString();

    return {
      ...options,
      since,
      until,
    };
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

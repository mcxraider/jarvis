/**
 * Service for direct integration with Todoist REST API
 * Handles all Todoist operations without relying on MCP servers
 *
 * @module TodoistAPIService
 */

import { logger } from '../../utils/logger';

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

/**
 * Service class for direct Todoist API integration
 */
export class TodoistAPIService {
  private readonly apiKey: string;
  private readonly baseURL = 'https://api.todoist.com/rest/v2';

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
  ): Promise<any> {
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
      logger.debug('Making Todoist API request', {
        url,
        method,
        hasBody: !!body,
      });

      const response = await fetch(url, requestOptions);

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`Todoist API error (${response.status}): ${errorText}`);
      }

      // Handle empty responses (like DELETE requests)
      if (response.status === 204 || response.headers.get('content-length') === '0') {
        return null;
      }

      const data = await response.json();

      logger.debug('Todoist API response received', {
        url,
        status: response.status,
        hasData: !!data,
      });

      return data;
    } catch (error) {
      logger.error('Todoist API request failed', {
        url,
        method,
        error: (error as Error).message,
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
  async addTask(payload: CreateTaskPayload): Promise<TodoistTask> {
    logger.info('Creating Todoist task', {
      content: payload.content,
      priority: payload.priority,
      hasDescription: !!payload.description,
    });

    const task = await this.makeRequest('/tasks', 'POST', payload);

    logger.info('Todoist task created successfully', {
      taskId: task.id,
      content: task.content,
    });

    return task;
  }

  /**
   * Get a specific task by ID
   *
   * @param taskId - Task ID to retrieve
   * @returns Promise<TodoistTask> - Task details
   */
  async getTask(taskId: string): Promise<TodoistTask> {
    logger.debug('Fetching Todoist task', { taskId });

    const task = await this.makeRequest(`/tasks/${taskId}`);

    logger.debug('Todoist task retrieved', {
      taskId: task.id,
      content: task.content,
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
  ): Promise<TodoistTask[]> {
    logger.debug('Fetching Todoist tasks', options);

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
    const tasks = await this.makeRequest(endpoint);

    logger.debug('Todoist tasks retrieved', {
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
  async updateTask(taskId: string, payload: UpdateTaskPayload): Promise<TodoistTask> {
    logger.info('Updating Todoist task', {
      taskId,
      hasContent: !!payload.content,
      priority: payload.priority,
    });

    const task = await this.makeRequest(`/tasks/${taskId}`, 'PUT', payload);

    logger.info('Todoist task updated successfully', {
      taskId: task.id,
      content: task.content,
    });

    return task;
  }

  /**
   * Mark a task as completed
   *
   * @param taskId - Task ID to complete
   * @returns Promise<void>
   */
  async completeTask(taskId: string): Promise<void> {
    logger.info('Completing Todoist task', { taskId });

    await this.makeRequest(`/tasks/${taskId}/close`, 'POST');

    logger.info('Todoist task completed successfully', { taskId });
  }

  /**
   * Delete a task permanently
   *
   * @param taskId - Task ID to delete
   * @returns Promise<void>
   */
  async deleteTask(taskId: string): Promise<void> {
    logger.info('Deleting Todoist task', { taskId });

    await this.makeRequest(`/tasks/${taskId}`, 'DELETE');

    logger.info('Todoist task deleted successfully', { taskId });
  }

  /**
   * Get all projects
   *
   * @returns Promise<TodoistProject[]> - Array of projects
   */
  async getProjects(): Promise<TodoistProject[]> {
    logger.debug('Fetching Todoist projects');

    const projects = await this.makeRequest('/projects');

    logger.debug('Todoist projects retrieved', {
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

  /**
   * Get completed tasks (Note: This requires Todoist Premium)
   * Uses the Sync API for retrieving completed tasks
   *
   * @param options - Query options
   * @returns Promise<any[]> - Array of completed tasks
   */
  async getCompletedTasks(
    options: {
      since?: string;
      until?: string;
      project_id?: string;
      limit?: number;
      offset?: number;
    } = {},
  ): Promise<any[]> {
    logger.debug('Fetching completed Todoist tasks', options);

    // Note: This uses the Sync API endpoint for completed tasks
    const syncUrl = 'https://api.todoist.com/sync/v9/completed/get_all';

    const params = new URLSearchParams();
    if (options.since) params.append('since', options.since);
    if (options.until) params.append('until', options.until);
    if (options.project_id) params.append('project_id', options.project_id);
    if (options.limit) params.append('limit', options.limit.toString());
    if (options.offset) params.append('offset', options.offset.toString());

    const fullUrl = `${syncUrl}${params.toString() ? '?' + params.toString() : ''}`;

    try {
      const response = await fetch(fullUrl, {
        method: 'GET',
        headers: {
          Authorization: `Bearer ${this.apiKey}`,
        },
      });

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`Todoist Sync API error (${response.status}): ${errorText}`);
      }

      const data = await response.json();

      logger.debug('Completed Todoist tasks retrieved', {
        count: data.items?.length || 0,
      });

      return data.items || [];
    } catch (error) {
      logger.error('Failed to fetch completed tasks', {
        error: (error as Error).message,
      });
      throw error;
    }
  }
}

"use strict";
/**
 * Service for direct integration with Todoist REST API
 * Handles all Todoist operations without relying on MCP servers
 *
 * @module TodoistAPIService
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.TodoistAPIService = void 0;
const logger_1 = require("../../utils/logger");
/**
 * Service class for direct Todoist API integration
 */
class TodoistAPIService {
    constructor(apiKey) {
        this.baseURL = 'https://api.todoist.com/rest/v2';
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
    async makeRequest(endpoint, method = 'GET', body) {
        const url = `${this.baseURL}${endpoint}`;
        const headers = {
            Authorization: `Bearer ${this.apiKey}`,
            'Content-Type': 'application/json',
        };
        const requestOptions = {
            method,
            headers,
        };
        if (body && (method === 'POST' || method === 'PUT')) {
            requestOptions.body = JSON.stringify(body);
        }
        try {
            logger_1.logger.debug('Making Todoist API request', {
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
            logger_1.logger.debug('Todoist API response received', {
                url,
                status: response.status,
                hasData: !!data,
            });
            return data;
        }
        catch (error) {
            logger_1.logger.error('Todoist API request failed', {
                url,
                method,
                error: error.message,
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
    async addTask(payload) {
        logger_1.logger.info('Creating Todoist task', {
            content: payload.content,
            priority: payload.priority,
            hasDescription: !!payload.description,
        });
        const task = await this.makeRequest('/tasks', 'POST', payload);
        logger_1.logger.info('Todoist task created successfully', {
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
    async getTask(taskId) {
        logger_1.logger.debug('Fetching Todoist task', { taskId });
        const task = await this.makeRequest(`/tasks/${taskId}`);
        logger_1.logger.debug('Todoist task retrieved', {
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
    async getTasks(options = {}) {
        logger_1.logger.debug('Fetching Todoist tasks', options);
        const params = new URLSearchParams();
        if (options.project_id)
            params.append('project_id', options.project_id);
        if (options.section_id)
            params.append('section_id', options.section_id);
        if (options.label)
            params.append('label', options.label);
        if (options.filter)
            params.append('filter', options.filter);
        if (options.lang)
            params.append('lang', options.lang);
        if (options.ids && options.ids.length > 0) {
            params.append('ids', options.ids.join(','));
        }
        const endpoint = `/tasks${params.toString() ? '?' + params.toString() : ''}`;
        const tasks = await this.makeRequest(endpoint);
        logger_1.logger.debug('Todoist tasks retrieved', {
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
    async updateTask(taskId, payload) {
        logger_1.logger.info('Updating Todoist task', {
            taskId,
            hasContent: !!payload.content,
            priority: payload.priority,
        });
        const task = await this.makeRequest(`/tasks/${taskId}`, 'PUT', payload);
        logger_1.logger.info('Todoist task updated successfully', {
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
    async completeTask(taskId) {
        logger_1.logger.info('Completing Todoist task', { taskId });
        await this.makeRequest(`/tasks/${taskId}/close`, 'POST');
        logger_1.logger.info('Todoist task completed successfully', { taskId });
    }
    /**
     * Delete a task permanently
     *
     * @param taskId - Task ID to delete
     * @returns Promise<void>
     */
    async deleteTask(taskId) {
        logger_1.logger.info('Deleting Todoist task', { taskId });
        await this.makeRequest(`/tasks/${taskId}`, 'DELETE');
        logger_1.logger.info('Todoist task deleted successfully', { taskId });
    }
    /**
     * Get all projects
     *
     * @returns Promise<TodoistProject[]> - Array of projects
     */
    async getProjects() {
        logger_1.logger.debug('Fetching Todoist projects');
        const projects = await this.makeRequest('/projects');
        logger_1.logger.debug('Todoist projects retrieved', {
            count: projects.length,
        });
        return projects;
    }
    /**
     * Get completed tasks (Note: This requires Todoist Premium)
     * Uses the Sync API for retrieving completed tasks
     *
     * @param options - Query options
     * @returns Promise<any[]> - Array of completed tasks
     */
    async getCompletedTasks(options = {}) {
        var _a;
        logger_1.logger.debug('Fetching completed Todoist tasks', options);
        // Note: This uses the Sync API endpoint for completed tasks
        const syncUrl = 'https://api.todoist.com/sync/v9/completed/get_all';
        const params = new URLSearchParams();
        if (options.since)
            params.append('since', options.since);
        if (options.until)
            params.append('until', options.until);
        if (options.project_id)
            params.append('project_id', options.project_id);
        if (options.limit)
            params.append('limit', options.limit.toString());
        if (options.offset)
            params.append('offset', options.offset.toString());
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
            logger_1.logger.debug('Completed Todoist tasks retrieved', {
                count: ((_a = data.items) === null || _a === void 0 ? void 0 : _a.length) || 0,
            });
            return data.items || [];
        }
        catch (error) {
            logger_1.logger.error('Failed to fetch completed tasks', {
                error: error.message,
            });
            throw error;
        }
    }
}
exports.TodoistAPIService = TodoistAPIService;

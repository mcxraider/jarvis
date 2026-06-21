import { GPTToolsService } from '../../../../src/services/tools/todoist-tools.service';
import { ToolDispatcher } from '../../../../src/types/tool.types';

const createDispatcher = (): ToolDispatcher => ({
  executeToolCalls: jest.fn(),
  isFunctionSupported: jest.fn(),
});

const getTool = (service: GPTToolsService, name: string) => {
  const tool = service.getToolByName(name);
  if (!tool) {
    throw new Error(`Expected tool ${name} to be defined`);
  }
  return tool;
};

const getProperties = (service: GPTToolsService, name: string) => {
  const parameters = getTool(service, name).function.parameters as {
    properties: Record<string, unknown>;
    required?: string[];
  };

  return parameters.properties;
};

describe('GPTToolsService', () => {
  it('returns no tools when no dispatcher is supplied', () => {
    const service = new GPTToolsService();

    expect(service.getAvailableTools()).toEqual([]);
    expect(service.getAvailableFunctionNames()).toEqual([]);
    expect(service.isFunctionSupported('add_todoist_task')).toBe(false);
  });

  it('exposes the expected Todoist tool names when a dispatcher is supplied', () => {
    const service = new GPTToolsService(createDispatcher());

    expect(service.getAvailableFunctionNames()).toEqual([
      'add_todoist_task',
      'get_todoist_task',
      'update_todoist_task',
      'delete_todoist_task',
      'get_completed_todoist_tasks_by_completion_date',
      'get_tasks',
      'get_tasks_by_filter',
      'complete_task',
    ]);
  });

  it('uses Todoist API priority semantics for task creation', () => {
    const service = new GPTToolsService(createDispatcher());
    const properties = getProperties(service, 'add_todoist_task');
    const priority = properties.priority as { enum: number[]; description: string };

    expect(priority.enum).toEqual([1, 2, 3, 4]);
    expect(priority.description).toContain('1 (normal / no priority)');
    expect(priority.description).toContain('4 (high / urgent)');
    expect(priority.description).not.toContain('1 (urgent)');
    expect(priority.description).not.toContain('4 (normal)');
  });

  it('uses Todoist API priority semantics for task updates', () => {
    const service = new GPTToolsService(createDispatcher());
    const properties = getProperties(service, 'update_todoist_task');
    const priority = properties.priority as { enum: number[]; description: string };

    expect(priority.enum).toEqual([1, 2, 3, 4]);
    expect(priority.description).toContain('1 (normal / no priority)');
    expect(priority.description).toContain('4 (high / urgent)');
    expect(priority.description).not.toContain('1 (urgent)');
    expect(priority.description).not.toContain('4 (normal)');
  });

  it('keeps required fields unchanged', () => {
    const service = new GPTToolsService(createDispatcher());
    const createParameters = getTool(service, 'add_todoist_task').function.parameters as {
      required?: string[];
    };
    const updateParameters = getTool(service, 'update_todoist_task').function.parameters as {
      required?: string[];
    };

    expect(createParameters.required).toEqual(['content']);
    expect(updateParameters.required).toEqual(['task_id']);
  });

  it('disallows additional properties on all tool parameters', () => {
    const service = new GPTToolsService(createDispatcher());

    service.getAvailableTools().forEach((tool) => {
      const parameters = tool.function.parameters as {
        additionalProperties?: boolean;
      };

      expect(parameters.additionalProperties).toBe(false);
    });
  });

  it('defines an explicit empty required array for get_tasks', () => {
    const service = new GPTToolsService(createDispatcher());
    const parameters = getTool(service, 'get_tasks').function.parameters as {
      required?: string[];
    };

    expect(parameters.required).toEqual([]);
  });

  it('separates structured task listing from filter queries', () => {
    const service = new GPTToolsService(createDispatcher());
    const listProperties = getProperties(service, 'get_tasks');
    const filterParameters = getTool(service, 'get_tasks_by_filter').function.parameters as {
      required?: string[];
    };

    expect(listProperties).not.toHaveProperty('filter');
    expect(listProperties).not.toHaveProperty('lang');
    expect(listProperties).toHaveProperty('parent_id');
    expect(listProperties).toHaveProperty('goal_id');
    expect(listProperties).toHaveProperty('cursor');
    expect(listProperties).toHaveProperty('limit');
    expect(filterParameters.required).toEqual(['query']);
  });
});

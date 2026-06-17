import { ZodError } from 'zod';
import { ToolCall } from '../../types/tool.types';
import {
  todoistToolArgumentSchemas,
  TodoistToolArguments,
  TodoistToolName,
} from './todoist-tool-schemas';

export type ValidatedToolCallArguments = {
  functionName: TodoistToolName;
  arguments: TodoistToolArguments;
};

/**
 * Validates model-generated tool call arguments before any side effects happen.
 */
export class ToolArgumentValidatorService {
  validate(toolCall: ToolCall): ValidatedToolCallArguments {
    const functionName = toolCall.function.name;

    if (!this.isTodoistToolName(functionName)) {
      throw new Error(`Unknown function: ${functionName}`);
    }

    const parsed = this.parseArguments(toolCall);
    const result = todoistToolArgumentSchemas[functionName].safeParse(parsed);

    if (!result.success) {
      throw new Error(
        `Invalid arguments for ${functionName}: ${this.formatZodError(result.error)}`,
      );
    }

    return {
      functionName,
      arguments: result.data,
    };
  }

  private parseArguments(toolCall: ToolCall): unknown {
    try {
      return JSON.parse(toolCall.function.arguments);
    } catch (error) {
      throw new Error(
        `Invalid JSON arguments for ${toolCall.function.name}: ${(error as Error).message}`,
      );
    }
  }

  private isTodoistToolName(functionName: string): functionName is TodoistToolName {
    return Object.prototype.hasOwnProperty.call(todoistToolArgumentSchemas, functionName);
  }

  private formatZodError(error: ZodError): string {
    return error.issues
      .map((issue) => {
        const path = issue.path.length > 0 ? issue.path.join('.') : 'root';
        return `${path}: ${issue.message}`;
      })
      .join('; ');
  }
}

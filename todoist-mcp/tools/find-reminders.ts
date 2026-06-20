import type {
    GetLocationRemindersArgs,
    GetLocationRemindersResponse,
    GetRemindersArgs,
    GetRemindersResponse,
    LocationReminder,
    Reminder,
} from '@doist/todoist-sdk'
import { z } from 'zod'
import type { TodoistTool } from '../todoist-tool.js'
import { countRemindersByType, fetchAllPages, mapReminder } from '../tool-helpers.js'
import { ReminderSchema as ReminderOutputSchema } from '../utils/output-schemas.js'
import { ToolNames } from '../utils/tool-names.js'

const ArgsSchema = {
    taskId: z
        .string()
        .optional()
        .describe(
            'The Todoist task ID whose reminders should be listed. Returns both time-based and location reminders for that task.',
        ),
    reminderId: z
        .string()
        .optional()
        .describe(
            'The ID of a specific time-based reminder to fetch. Use this for relative and absolute reminders, not location reminders.',
        ),
    locationReminderId: z
        .string()
        .optional()
        .describe(
            'The ID of a specific location reminder to fetch. Use this only for geofence reminders.',
        ),
}

const OutputSchema = {
    reminders: z
        .array(ReminderOutputSchema)
        .describe('The found reminders (time-based and location).'),
    searchType: z
        .string()
        .describe('The search type used: "task", "reminder", or "location_reminder".'),
    searchId: z.string().describe('The ID used for the search.'),
    totalCount: z.number().describe('Total reminders in this response.'),
}

const findReminders = {
    name: ToolNames.FIND_REMINDERS,
    description:
        'Finds Todoist reminders without modifying them. Use this when the user asks what reminders exist for a task, or when you need the exact reminder ID before updating or deleting a reminder. Provide exactly one lookup parameter: taskId to list all reminders on a task, reminderId for a time-based reminder, or locationReminderId for a location reminder. The tool returns reminder objects, the lookup type used, the searched ID, and a total count.',
    parameters: ArgsSchema,
    outputSchema: OutputSchema,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true },
    async execute(args, client) {
        const { taskId, reminderId, locationReminderId } = args

        // Validate exactly one parameter is provided
        const providedParams = [taskId, reminderId, locationReminderId].filter(Boolean)
        if (providedParams.length === 0) {
            throw new Error('One of taskId, reminderId, or locationReminderId must be provided.')
        }
        if (providedParams.length > 1) {
            throw new Error(
                'Only one of taskId, reminderId, or locationReminderId can be provided at a time.',
            )
        }

        if (reminderId) {
            const reminder = await client.getReminder(reminderId)
            const mapped = mapReminder(reminder)

            return {
                textContent: `Found ${reminder.type} reminder (id=${reminderId})`,
                structuredContent: {
                    reminders: [mapped],
                    searchType: 'reminder',
                    searchId: reminderId,
                    totalCount: 1,
                },
            }
        }

        if (locationReminderId) {
            const reminder = await client.getLocationReminder(locationReminderId)
            const mapped = mapReminder(reminder)

            return {
                textContent: `Found location reminder (id=${locationReminderId})`,
                structuredContent: {
                    reminders: [mapped],
                    searchType: 'location_reminder',
                    searchId: locationReminderId,
                    totalCount: 1,
                },
            }
        }

        if (taskId) {
            // taskId search: fetch both time-based and location reminders in parallel
            const [timeBasedReminders, locationReminders] = await Promise.all([
                fetchAllPages<GetRemindersArgs, GetRemindersResponse, Reminder>({
                    apiMethod: (args) => client.getReminders(args),
                    args: { taskId },
                }),
                fetchAllPages<
                    GetLocationRemindersArgs,
                    GetLocationRemindersResponse,
                    LocationReminder
                >({
                    apiMethod: (args) => client.getLocationReminders(args),
                    args: { taskId },
                }),
            ])

            const allReminders: Reminder[] = [...timeBasedReminders, ...locationReminders]
            const mappedReminders = allReminders.map(mapReminder)

            const textContent = generateTextContent(mappedReminders, taskId)

            return {
                textContent,
                structuredContent: {
                    reminders: mappedReminders,
                    searchType: 'task',
                    searchId: taskId,
                    totalCount: mappedReminders.length,
                },
            }
        }

        throw new Error('One of taskId, reminderId, or locationReminderId must be provided.')
    },
} satisfies TodoistTool<typeof ArgsSchema, typeof OutputSchema>

function generateTextContent(reminders: ReturnType<typeof mapReminder>[], taskId: string): string {
    if (reminders.length === 0) {
        return `No reminders found for task ${taskId}`
    }

    const { timeBasedCount, locationCount } = countRemindersByType(reminders)

    const parts: string[] = []
    if (timeBasedCount > 0) {
        const label = timeBasedCount > 1 ? 'time-based reminders' : 'time-based reminder'
        parts.push(`${timeBasedCount} ${label}`)
    }
    if (locationCount > 0) {
        const label = locationCount > 1 ? 'location reminders' : 'location reminder'
        parts.push(`${locationCount} ${label}`)
    }

    return `Found ${parts.join(' and ')} for task ${taskId}`
}

export { findReminders }

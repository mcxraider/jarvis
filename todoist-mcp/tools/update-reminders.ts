import { z } from 'zod'
import type { TodoistTool } from '../todoist-tool.js'
import { mapReminder } from '../tool-helpers.js'
import { ReminderSchema as ReminderOutputSchema } from '../utils/output-schemas.js'
import {
    LocationTriggerSchema,
    MAX_REMINDERS_PER_OPERATION,
    ReminderDueInputSchema,
    ReminderIsUrgentSchema,
    ReminderServiceSchema,
} from '../utils/reminder-schemas.js'
import { ToolNames } from '../utils/tool-names.js'

const RelativeReminderUpdateSchema = z.object({
    type: z.literal('relative'),
    id: z
        .string()
        .min(1)
        .describe(
            'The ID of the relative reminder to update. Use find-reminders first if you only know the task.',
        ),
    minuteOffset: z
        .number()
        .int()
        .min(0)
        .optional()
        .describe('New minute offset before task due time.'),
    service: ReminderServiceSchema.optional().describe('New delivery method: "email" or "push".'),
    isUrgent: ReminderIsUrgentSchema,
})

const AbsoluteReminderUpdateSchema = z.object({
    type: z.literal('absolute'),
    id: z
        .string()
        .min(1)
        .describe(
            'The ID of the absolute reminder to update. Use find-reminders first if you only know the task.',
        ),
    due: ReminderDueInputSchema.optional().describe(
        'New date/time for the reminder, either as an explicit date/time payload or a natural language string with optional timezone.',
    ),
    service: ReminderServiceSchema.optional().describe('New delivery method: "email" or "push".'),
    isUrgent: ReminderIsUrgentSchema,
})

const LocationReminderUpdateSchema = z.object({
    type: z.literal('location'),
    id: z
        .string()
        .min(1)
        .describe(
            'The ID of the location reminder to update. Use find-reminders first if you only know the task.',
        ),
    name: z.string().optional().describe('New location name.'),
    locLat: z.string().optional().describe('New latitude.'),
    locLong: z.string().optional().describe('New longitude.'),
    locTrigger: LocationTriggerSchema.optional().describe(
        'New trigger condition: "on_enter" or "on_leave".',
    ),
    radius: z.number().int().optional().describe('New radius in meters.'),
})

const ReminderUpdateSchema = z.discriminatedUnion('type', [
    RelativeReminderUpdateSchema,
    AbsoluteReminderUpdateSchema,
    LocationReminderUpdateSchema,
])

const ArgsSchema = {
    reminders: z
        .array(ReminderUpdateSchema)
        .min(1)
        .max(MAX_REMINDERS_PER_OPERATION)
        .describe(
            `Array of reminders to update (max ${MAX_REMINDERS_PER_OPERATION}). Each must include the reminder type and ID. Only include fields that need to change.`,
        ),
}

const OutputSchema = {
    reminders: z.array(ReminderOutputSchema).describe('The updated reminders.'),
    totalCount: z.number().describe('Total reminders updated.'),
    updatedReminderIds: z.array(z.string()).describe('IDs of updated reminders.'),
}

const updateReminders = {
    name: ToolNames.UPDATE_REMINDERS,
    description:
        'Updates one or more existing Todoist reminders by reminder ID. Use this after finding the reminder ID when the user wants to change reminder timing, delivery service, urgency, or location trigger details. Each update must include the reminder type because Todoist uses separate APIs for time-based and location reminders; include only fields that should change. Do not use this to create reminders or delete reminders; use add-reminders to create them and delete-object with type "reminder" or "location_reminder" to remove them. The tool returns the updated reminder objects, their IDs, and the total number updated.',
    parameters: ArgsSchema,
    outputSchema: OutputSchema,
    annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: true },
    async execute(args, client) {
        const { reminders } = args

        const updatePromises = reminders.map(async (reminder) => {
            switch (reminder.type) {
                case 'relative': {
                    const { id, type: _, ...updateArgs } = reminder
                    return await client.updateReminder(id, {
                        reminderType: 'relative',
                        ...updateArgs,
                    })
                }
                case 'absolute': {
                    const { id, type: _, ...updateArgs } = reminder
                    return await client.updateReminder(id, {
                        reminderType: 'absolute',
                        ...updateArgs,
                    })
                }
                case 'location': {
                    const { id, type: _, ...updateArgs } = reminder
                    return await client.updateLocationReminder(id, updateArgs)
                }
            }
        })

        const updatedReminders = await Promise.all(updatePromises)
        const mappedReminders = updatedReminders.map(mapReminder)

        const label = mappedReminders.length === 1 ? 'reminder' : 'reminders'
        const textContent = `Updated ${mappedReminders.length} ${label}`

        return {
            textContent,
            structuredContent: {
                reminders: mappedReminders,
                totalCount: mappedReminders.length,
                updatedReminderIds: mappedReminders.map((r) => r.id),
            },
        }
    },
} satisfies TodoistTool<typeof ArgsSchema, typeof OutputSchema>

export { updateReminders }

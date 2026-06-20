import { z } from 'zod'
import type { TodoistTool } from '../todoist-tool.js'
import { mapComment, mapGoal, mapProject, mapTask } from '../tool-helpers.js'
import {
    CommentSchema,
    GoalSchema,
    ProjectSchema,
    SectionSchema,
    TaskSchema,
    toSectionSummary,
} from '../utils/output-schemas.js'
import { ToolNames } from '../utils/tool-names.js'

const ObjectTypes = ['task', 'project', 'comment', 'section', 'goal'] as const

const ArgsSchema = {
    type: z
        .enum(ObjectTypes)
        .describe(
            'The kind of Todoist object to fetch. Choose the type that matches the ID you already have: task, project, comment, section, or goal.',
        ),
    id: z
        .string()
        .min(1)
        .describe(
            'The exact Todoist object ID to retrieve. Use a find-* tool first if you only know a name, title, or search phrase.',
        ),
}

const OutputSchema = {
    type: z.enum(ObjectTypes).describe('The type of object fetched.'),
    id: z.string().describe('The ID of the fetched object.'),
    object: z
        .union([TaskSchema, ProjectSchema, CommentSchema, SectionSchema, GoalSchema])
        .describe('The fetched object data.'),
}

const fetchObject = {
    name: ToolNames.FETCH_OBJECT,
    description:
        'Fetches one specific Todoist object by type and ID without modifying it. Use this when you already have an ID and need the current details for a task, project, comment, section, or goal before deciding what to do next. Do not use this for search by name, text, date, label, or project membership; use the relevant find-* tool for discovery. The tool returns the requested object in a typed structured payload, or raises an error if the ID does not exist or does not match the selected type.',
    parameters: ArgsSchema,
    outputSchema: OutputSchema,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true },
    async execute(args, client) {
        const { type, id } = args

        try {
            switch (type) {
                case 'task': {
                    const task = await client.getTask(id)
                    const mappedTask = mapTask(task)
                    return {
                        textContent: `Found task: ${mappedTask.content} • id=${mappedTask.id} • priority=${mappedTask.priority} • project=${mappedTask.projectId}`,
                        structuredContent: {
                            type,
                            id,
                            object: mappedTask,
                        },
                    }
                }
                case 'project': {
                    const project = await client.getProject(id)
                    const mappedProject = mapProject(project)
                    return {
                        textContent: `Found project: ${mappedProject.name} • id=${mappedProject.id} • color=${mappedProject.color} • viewStyle=${mappedProject.viewStyle}`,
                        structuredContent: {
                            type,
                            id,
                            object: mappedProject,
                        },
                    }
                }
                case 'comment': {
                    const comment = await client.getComment(id)
                    const mappedComment = mapComment(comment)
                    const truncatedContent =
                        mappedComment.content.length > 50
                            ? `${mappedComment.content.substring(0, 50)}...`
                            : mappedComment.content
                    return {
                        textContent: `Found comment • id=${mappedComment.id} • content="${truncatedContent}" • posted=${mappedComment.postedAt}`,
                        structuredContent: {
                            type,
                            id,
                            object: mappedComment,
                        },
                    }
                }
                case 'section': {
                    const section = await client.getSection(id)

                    if (!section) {
                        throw new Error(`Section ${id} not found.`)
                    }

                    const mappedSection = toSectionSummary(section)
                    return {
                        textContent: `Found section: ${mappedSection.name} • id=${mappedSection.id}`,
                        structuredContent: {
                            type,
                            id,
                            object: mappedSection,
                        },
                    }
                }
                case 'goal': {
                    const goal = await client.getGoal(id)
                    const mappedGoal = mapGoal(goal)
                    return {
                        textContent: `Found goal: ${mappedGoal.name} • id=${mappedGoal.id} • progress=${mappedGoal.progress?.percentage ?? 0}%`,
                        structuredContent: {
                            type,
                            id,
                            object: mappedGoal,
                        },
                    }
                }
            }
        } catch (error) {
            throw new Error(
                `Failed to fetch ${type} with id ${id}: ${error instanceof Error ? error.message : String(error)}`,
            )
        }
    },
} satisfies TodoistTool<typeof ArgsSchema, typeof OutputSchema>

export { fetchObject }

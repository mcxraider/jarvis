import { z } from 'zod'
import type { TodoistTool } from '../todoist-tool.js'
import { mapProject } from '../tool-helpers.js'
import { ProjectSchema } from '../utils/output-schemas.js'
import { ToolNames } from '../utils/tool-names.js'

const ArgsSchema = {
    action: z
        .enum(['archive', 'unarchive'])
        .describe(
            'Whether to archive the project, hiding it from active project lists, or unarchive it so it becomes active again.',
        ),
    projectId: z
        .string()
        .min(1)
        .describe(
            'The Todoist project ID to archive or unarchive. Use find-projects or fetch-object first if you only know the project name.',
        ),
}

const OutputSchema = {
    project: ProjectSchema.describe('The updated project.'),
    success: z.boolean().describe('Whether the action was successful.'),
}

const projectManagement = {
    name: ToolNames.PROJECT_MANAGEMENT,
    description:
        'Archives or unarchives an existing Todoist project by ID. Use this when the user explicitly wants to hide a project from active use or restore an archived project. Do not use this to rename, recolor, reorder, move, or delete projects; use update-projects, reorder-objects, project-move, or delete-object for those operations. The tool returns the updated project object and a success flag.',
    parameters: ArgsSchema,
    outputSchema: OutputSchema,
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true },
    async execute(args, client) {
        const project =
            args.action === 'archive'
                ? await client.archiveProject(args.projectId)
                : await client.unarchiveProject(args.projectId)

        const mappedProject = mapProject(project)

        return {
            textContent: `${args.action === 'archive' ? 'Archived' : 'Unarchived'} project: ${mappedProject.name} (id=${mappedProject.id})`,
            structuredContent: {
                project: mappedProject,
                success: true,
            },
        }
    },
} satisfies TodoistTool<typeof ArgsSchema, typeof OutputSchema>

export { projectManagement }

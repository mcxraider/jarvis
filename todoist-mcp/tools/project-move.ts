import type {
    MoveProjectToWorkspaceArgs,
    PersonalProject,
    WorkspaceProject,
} from '@doist/todoist-sdk'
import { z } from 'zod'
import type { TodoistTool } from '../todoist-tool.js'
import { mapProject } from '../tool-helpers.js'
import { ProjectSchema } from '../utils/output-schemas.js'
import { ToolNames } from '../utils/tool-names.js'

const ArgsSchema = {
    action: z
        .enum(['move-to-workspace', 'move-to-personal'])
        .describe(
            'Choose "move-to-workspace" to convert or place a personal project in a workspace, or "move-to-personal" to move a workspace project back to personal projects.',
        ),
    projectId: z
        .string()
        .min(1)
        .describe(
            'The Todoist project ID to move. Use find-projects first if you only know the project name.',
        ),
    workspaceId: z
        .string()
        .min(1)
        .optional()
        .describe(
            'The target workspace ID. Required when action is "move-to-workspace"; omit it when moving a project to personal.',
        ),
    folderId: z
        .string()
        .min(1)
        .optional()
        .describe(
            'Optional target folder ID within the workspace. Only applies when moving a project into a workspace.',
        ),
    visibility: z
        .enum(['restricted', 'team', 'public'])
        .optional()
        .describe(
            'Optional access visibility for the project in the workspace (restricted, team, or public).',
        ),
}

const OutputSchema = {
    project: ProjectSchema.describe('The moved project.'),
    success: z.boolean().describe('Whether the move was successful.'),
}

const projectMove = {
    name: ToolNames.PROJECT_MOVE,
    description:
        'Moves a Todoist project between personal projects and workspace projects. Use this when the user wants to change project ownership/context, optionally placing the project into a workspace folder or setting workspace visibility. Do not use this for reordering projects within the same location; use reorder-objects for sibling order or parent changes. The tool returns the moved project object and a success flag.',
    parameters: ArgsSchema,
    outputSchema: OutputSchema,
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true },
    async execute(args, client) {
        let project: PersonalProject | WorkspaceProject

        if (args.action === 'move-to-workspace') {
            if (!args.workspaceId) {
                throw new Error('workspaceId is required when action is move-to-workspace')
            }

            const moveArgs: MoveProjectToWorkspaceArgs = {
                projectId: args.projectId,
                workspaceId: args.workspaceId,
            }

            if (args.folderId) {
                moveArgs.folderId = args.folderId
            }

            if (args.visibility) {
                moveArgs.access = { visibility: args.visibility }
            }

            project = await client.moveProjectToWorkspace(moveArgs)
        } else {
            project = await client.moveProjectToPersonal({ projectId: args.projectId })
        }

        const mappedProject = mapProject(project)

        const actionText =
            args.action === 'move-to-workspace' ? 'Moved to workspace' : 'Moved to personal'

        return {
            textContent: `${actionText}: ${mappedProject.name} (id=${mappedProject.id})`,
            structuredContent: {
                project: mappedProject,
                success: true,
            },
        }
    },
} satisfies TodoistTool<typeof ArgsSchema, typeof OutputSchema>

export { projectMove }

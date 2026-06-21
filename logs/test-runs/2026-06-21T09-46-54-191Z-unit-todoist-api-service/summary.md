# unit-todoist-api-service

Status: completed
Finished: 2026-06-21T09:46:54.205Z

## Events

- 2026-06-21T09:46:54.194Z [request] addTask: {"url":"https://api.todoist.com/api/v1/tasks","options":{"method":"POST","headers":{"Authorization":"[REDACTED]","Content-Type":"application/json"},"body":"{\"content\":\"Review invoices\",\"due_string\":\"tomorrow at 9am\",\"priority\":4,\"labels\":[\"jarvis-test\"]}"}}
- 2026-06-21T09:46:54.195Z [request] getTask: {"call":["https://api.todoist.com/api/v1/tasks/task-1",{"method":"GET","headers":{"Authorization":"[REDACTED]","Content-Type":"application/json"}}]}
- 2026-06-21T09:46:54.195Z [request] getTasks: {"url":"https://api.todoist.com/api/v1/tasks?project_id=project-1&section_id=section-1&parent_id=parent-1&label=jarvis-test&ids=1%2C2&goal_id=550e8400-e29b-41d4-a716-446655440000&cursor=page-2&limit=25"}
- 2026-06-21T09:46:54.196Z [request] updateTask: {"url":"https://api.todoist.com/api/v1/tasks/task-1","options":{"method":"POST","headers":{"Authorization":"[REDACTED]","Content-Type":"application/json"},"body":"{\"content\":\"Review invoices now\",\"priority\":4}"}}
- 2026-06-21T09:46:54.196Z [request] completeTask: {"call":["https://api.todoist.com/api/v1/tasks/task-1/close",{"method":"POST","headers":{"Authorization":"[REDACTED]","Content-Type":"application/json"}}]}
- 2026-06-21T09:46:54.196Z [request] deleteTask: {"call":["https://api.todoist.com/api/v1/tasks/task-1",{"method":"DELETE","headers":{"Authorization":"[REDACTED]","Content-Type":"application/json"}}]}
- 2026-06-21T09:46:54.197Z [request] getProjects: {"calls":[["https://api.todoist.com/api/v1/projects",{"method":"GET","headers":{"Authorization":"[REDACTED]","Content-Type":"application/json"}}],["https://api.todoist.com/api/v1/projects",{"method":"GET","headers":{"Authorization":"[REDACTED]","Content-Type":"application/json"}}]]}
- 2026-06-21T09:46:54.197Z [request] getCompletedTasks: {"url":"https://api.todoist.com/api/v1/tasks/completed/by_completion_date?since=2026-06-01T00%3A00%3A00Z&until=2026-06-16T23%3A59%3A59Z&workspace_id=123456&project_id=project-1&section_id=section-1&parent_id=parent-1&filter_query=%40work&filter_lang=en&cursor=page-2&limit=25","options":{"method":"GET","headers":{"Authorization":"[REDACTED]","Content-Type":"application/json"}}}
- 2026-06-21T09:46:54.205Z [response] apiFailures: {"restError":"Todoist API error (500): rest exploded","completedTasksError":"Todoist API error (403): completed denied"}

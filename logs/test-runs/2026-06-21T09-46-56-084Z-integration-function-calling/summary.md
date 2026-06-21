# integration-function-calling

Status: completed
Finished: 2026-06-21T09:46:56.090Z

## Events

- 2026-06-21T09:46:56.086Z [step] gpt_tool_call_flow: {"userMessage":"Add review invoices tomorrow at 9am with high priority","toolCalls":[{"id":"call-add","type":"function","function":{"name":"add_todoist_task","arguments":"{\"content\":\"Review invoices\",\"due_string\":\"tomorrow at 9am\",\"priority\":4}"}},{"id":"call-list","type":"function","function":{"name":"get_tasks_by_filter","arguments":"{\"query\":\"today\"}"}},{"id":"call-update","type":"function","function":{"name":"update_todoist_task","arguments":"{\"task_id\":\"task-1\",\"content\":\"Review invoices now\"}"}},{"id":"call-complete","type":"function","function":{"name":"complete_task","arguments":"{\"task_id\":\"task-1\"}"}},{"id":"call-delete","type":"function","function":{"name":"delete_todoist_task","arguments":"{\"task_id\":\"task-2\"}"}}],"finalResponse":"Done — 5 actions completed."}
- 2026-06-21T09:46:56.089Z [assertion] unsupported_tool_filtered: {"finalResponse":"Done."}

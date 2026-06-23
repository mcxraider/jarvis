# Ideas

### Graph
- For risky actions: Now user has to confirm/ decline. want to add another option to "chat instead".
- i think maybe need a planner node first, to map out the tasks needed, then this is sent to orchestrator to spawn workers. (use this query: Go through my tasks, check everything that does not have a time, that is also not a birthday. Tell me and I will ask you to make edits.) idk might need to do some claude research. Consider using a DAG structure: https://github.com/arunpshankar/Agentic-Workflow-Patterns/tree/main/src/patterns/dag_orchestration . This is possible in langgraph
- everytime u add, there should always be a conflict layer check that notifies the user and ask the user for a next step like do u wanna keep both? or do u wanna keep one. 


### Formatting (UI)
- Rich message formatting to user:
  - use TODOs in the planner stage, then stream it to the user and show it to him in "Task list item not completed" and when completed show in "Task list item completed" rich message style.
  - AI info Footnote in every final reply using heading 6 size text.

### Logging
- need better logging for transcription service

### UX
- user needs to know of some way to tell if he is being asked a clarification question so he knows if still current chat or new chat can be started with next query.


### Speech
- show user transcription, ask user to confirm/ reject transcription message.


### Integrations
- search tool: google maps
- zac - "if i say do xyz at shake shack at vivo" then i want it to add the actual location into the task as well.  
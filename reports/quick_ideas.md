# Ideas

- Rich message formatting to user:
  - use TODOs in the planner stage, then stream it to the user and show it to him in "Task list item not completed" and when completed show in "Task list item completed" rich message style.
  - AI info Footnote in every final reply using heading 6 size text.
- need better logging for transcription service
- user needs to know of some way to tell if he is being asked a clarification question so he knows if still current chat or new chat can be started with next query.
- i think maybe need a planner node first, to map out the tasks needed, then this is sent to orchestrator to spawn workers. (use this query: Go through my tasks, check everything that does not have a time, that is also not a birthday. Tell me and I will ask you to make edits.) idk might need to do some claude research. Consider using a DAG structure: https://github.com/arunpshankar/Agentic-Workflow-Patterns/tree/main/src/patterns/dag_orchestration . This is possible in langgraph
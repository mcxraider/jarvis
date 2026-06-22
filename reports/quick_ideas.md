# Ideas

- tracer for langsmith no longer tracking token count and no more verbose graph actions like deepseek response, user response, tool call etc etc.
- need better logging for transcription service
- need some sort of Goal node (so like if goal doesnt reach, then that node doesnt stop working or asking the user)
- need to abstract out tool selection service, future will be some regex/bm25 feature that narrows down tools from user query
- user needs to know of some way to tell if he is being asked a clarification question so he knows if still current chat or new chat can be started with next query.
- i think maybe need a planner node first, to map out the tasks needed, then this is sent to orchestrator to spawn workers. (use this query: Go through my tasks, check everything that does not have a time, that is also not a birthday. Tell me and I will ask you to make edits.) idk might need to do some claude research. Consider using a DAG structure: https://github.com/arunpshankar/Agentic-Workflow-Patterns/tree/main/src/patterns/dag_orchestration . This is possible in langgraph
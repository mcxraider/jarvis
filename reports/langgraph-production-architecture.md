# Future architecture

## Current state:
```mermaid
flowchart TD
    Start([New invocation]) --> Initial[build_initial_state]
    Resume([Resume with same thread_id]) --> Command[Command resume=reply]
    Initial --> Agent
    Command --> HITL

    Agent[agent node<br/>call DeepSeek and append response]
    Tools[tools node<br/>execute tool calls and append results]
    HITL[hitl node<br/>interrupt, then incorporate reply]
    End([END])

    Agent -->|ask_user tool call| HITL
    Agent -->|other tool calls| Tools
    Agent -->|plain response or error| End
    Tools --> Agent
    HITL --> Agent
```


## Ideal state
```mermaid
flowchart TD
    Start([New invocation]) --> Initial[build_initial_state]
    Resume([Resume with same thread_id]) --> Command[Command resume=reply]
    Initial --> Agent
    Command --> HITL
    Command --> Confirm

    Agent[agent node<br/>call DeepSeek and append response]
    Tools[tools node<br/>execute, or hold + flag risky mutation]
    HITL[hitl node<br/>interrupt for clarification, then incorporate reply]
    Confirm[confirm node<br/>interrupt for approve/decline, then incorporate reply]
    End([END])

    Agent -->|ask_user tool call| HITL
    Agent -->|tool_calls: other| Tools
    Agent -->|re-dispatch: pre-approved call| Tools
    Agent -->|plain response or error| End
    Tools -->|low risk: execute now, append result| Agent
    Tools -->|risky: delete / bulk write / parent w/ children<br/>hold execution| Confirm
    Confirm -->|approve or decline → append result| Agent
    HITL --> Agent
```

has to be abe to handle such difficult ones:
Go through my task, check everything that does not have a time, that is also not a birthday. Tell me and I will ask you to make edits.

Correct steps:
1. filter all tasks that is not birthday, and doesnt have time
2. ask back to the user what edits he wants.

currently pipeline can only do step 1.
to handle this maybe we need the dispatch workers, sequential? so filter everything, then ask back the user? idk, need to explore what workers means. or is there a more efficient way to handle this? maybe using more powerful V4 pro for orchestrator then V4 flash cheaper for workers?

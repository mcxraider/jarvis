# Ideas


### Reliability


### Safety



i am trying to build a new feature to enhance the agents capability for scaling
  up to new integrations in the future. First this is my idea:
### Agent capability enhancements
- query Router: pre-orchestrator
- deepseek-flash or groq api call, need something fairly low latency. Takes in the user preferences, connected services and returns the tools/domains required for this query. so the tool and contexts are loaded in appropriately at runtime. 
- so for a todoist example user says: "add task to jarvis mcp cal" -> model responds with the necessary info/payload fields to only signal to add in the todoist context, tips and tools into the orchestrator. for that prompt. for every subsequent prompt as well, it goes through the router, as one part the user might be asking for todoist but in the next prompt in same thread user might be asking for google calendar/ some other integration fetching as well, then system prompt needs to be edited to configure for both todoist and google calendar tools. 
- for another example like just google calendar: "fetch events from both phoebe medicine calendar and jerrys govtech calendar and find days which are good for us to meet", then only google calendar stuff is loaded in, and no todoist context is added in. The current model supports this already but currently todoist context is still being loaded in for these types of queries. 
- the user prompt and the properly loaded context is added in. this allows me to scale to different integrations quickly without having to worry.
- In the future i can even do like a tiered query router so based on this i can route different difficulty queries to different models (deepseek pro, openai etc) for better performance for more difficult tasks. 


please analyse how the contexts are curently being loaded in as it is currently
  quite robust already.
  also analyse how other small things can be done by the router (but not too
  much) so that performance of the agent increases as well. (a quick query
  rewrite as well? idk something like that)

  Dive into the codebase to explore the current methods of fetching and loading
  in context etc first.


### LLM

### Formatting (UI)


### Logging


### Tooling


### UX


### Speech


### User enhancements/ Integrations/ features

- GitHub issues: investigate whether Jarvis can read, create, and update issues in
  its GitHub repository.

### Telegram Bot

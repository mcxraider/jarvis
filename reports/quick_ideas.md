# Ideas


### Latency
- User query cache (need versioning for preferences schema etc as it may change or maybe everytime they edit preferences then need to scrub their cache). Do this using a hash(user preferences version  + query) cache

### Reliability
- what if user asks simultaneously from phone and web and desktop app?
- if user input is above a certain number of words, reject?


### Safety
- detect prompt attacks using rivalAI
- PII scrub?
- OpenAI Moderation API + simple regex/rules for prompt injection


### Agent capability enhancements
- query difficulty routing to different models (deterministic. 2 domain = flash, 3+ = pro)


### LLM
#### Query intelligence (maybe add this to the router features)
This is one of the more interesting decisions in the architecture. There are four things you need to know about a query before you can answer it well:
  - what the user is trying to do (intent)
  - whether the query contains multiple distinct questions that should be answered separately (sub-query decomposition)
  - how complex it is (which determines which model you’ll use)
  - whether it needs decomposition at all

### Formatting (UI)


### Logging


### Tooling


### UX
- better logging to the user on telegram:
  -> User asks: "When am i free next week" -> "Routing..." -> router 
    -> router determines domains 
      -> [Async] reply to user: "Got it — I’m loading Todoist/Google Calendar..."
      -> [Async] Orchestrator starts fetching
        -> [After router reply to user] "Preparing app tools..." -> "Running App request..." -> "Received App Response..."
          -> [orchestrator reply to user] model answer

### Speech


### User enhancements/ Integrations/ features


### Telegram Bot
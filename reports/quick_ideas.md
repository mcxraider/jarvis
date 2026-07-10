# Ideas

current fixes 
- also, if no date specified like "Check govtech events and do..." it should default to only searching for the forward 1 month (30 day) period not everything. 
- Router potential enhancements:
  - Measure router latency and include task decomposition
  - Using the router to ask_user before routing to orchestrator
- 


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
- when at the prepare_confirm stage, if user gives input instead of clicking Accept/ Decline, then it should route back to the model. With the correct context. Ie, the confirm message, and the users input. Need a way to check if another node should fix this because if model asks for prepare confirm, then the user gives like "yes, but change one part" then the model should redo the thing, fix it edit out that part, and clarify again. but if the guy says "yes all is good". then it should bypass the permissions and dont need to go prepare_confirm anymore because user already implied yes. 

### LLM


#### Query intelligence (maybe add this to the router features)


### Formatting (UI)


### Logging


### Tooling


### UX


### Speech


### User enhancements/ Integrations/ features


### Telegram Bot
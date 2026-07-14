# Ideas

current fixes 
- Also, if no date specified like "Check govtech events and do..." it should default to only searching for the forward 1 month (30 day) period not everything. is there a way to limit the number of things that can be fetched by the tool? 
- Audio messages are sent directly to the transcription service (Groq Whisper) without first-layer validation. Long recordings or oversized files could hit API limits, waste quota, or fail silently.
- Context window management. Strategy for if context window exceeds a certain point due to large tool calls, need a running number of tokens tracker. 
- 


### Db
- Setup OAuth process. Can just be code for now, run on my laptop. or rather, a script that i have. 
  - Setup Oauth script for: todoist, google calendar.


### User enhancements/ Integrations/ features
- the bot could take in images/ PDFs, where like each one is a possible like holiday itinerary or some schedule of some sort, like sch calendar?? Then it automaticaly creates the events and stuff for u
- journal feature. upload images etc into telegram along with a caption. Then can write to a daily entry journal on notion. (only support writes, not reads for now)


### Latency
- async for the agent stuff, is prompt building all async? 
- User query cache (need versioning for preferences schema etc as it may change or maybe everytime they edit preferences then need to scrub their cache). Do this using a hash(user preferences version  + query) cache

### Reliability
- if user input is above a certain number of words, need a decomposer, or some form of breakdown router. basically a router for the router. now currently its just V4 flash. 
- must have a round trip second call validator to check that the tool call resulted in actually deleting or adding to prevent model hallucinations. like after the user approves/ rejects then need to check that the tool call fired/ didnt fire. 

### Safety
- PII scrub?
- OpenAI Moderation API + simple regex/rules for prompt injection


### Agent capability enhancements
- when at the prepare_confirm stage, if user gives input instead of clicking Accept/ Decline, then it should route back to the model. With the correct context. Ie, the confirm message, and the users input. Need a way to check if another node should fix this because if model asks for prepare confirm, then the user gives like "yes, but change one part" then the model should redo the thing, fix it edit out that part, and clarify again. but if the guy says "yes all is good". then it should bypass the permissions and dont need to go prepare_confirm anymore because user already implied yes. 


### LLM


### Formatting (UI)


### Logging
- Anonymising users data. need strategy. maybe new logger queue. sent for PII scrubbing. 
  - So user run finishes: All metadata gets sent to the logger. Logging service is a queue that picks it up, post processes it with all the necesary scrubs, then logs it to the databases. 


### Tooling


### UX


### Speech


### Telegram Bot
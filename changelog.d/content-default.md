### Changed
- **Breaking:** framework handlers (`briefcase.auto("langchain")`, `"llamaindex"`, `"crewai"`,
  `"openai-agents"`, `"autogen"`, `"ag2"`, `"pageindex"`, and the handler classes themselves)
  record no prompt, completion or error text by default, like the provider integrations,
  `briefcase exec`, `connect`/the receiver and eval records. They take the providers' values
  (`capture_content="full"`/`True`, `"hash"`, `"none"`/`False`) and `BRIEFCASE_CAPTURE_CONTENT`
  through `auto()`. The one-time "records FULL content" warning is gone. `@briefcase.capture`
  is unchanged: decorating your own function is the opt-in and records its arguments and return
  value. Migration: `briefcase.auto("langchain", capture_content="full")` or
  `BRIEFCASE_CAPTURE_CONTENT=full`.

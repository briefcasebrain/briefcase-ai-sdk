# Run from the repo root: WHEEL=/path/to/briefcase_ai-*.whl vhs scripts/recordings/03-record-replay.tape
Output "/Users/aansh/briefcase-ws/recordings/sdk/03-record-replay.mp4"
Set Shell "bash"
Set FontSize 20
Set Width 1280
Set Height 720
Set Padding 16
Set TypingSpeed 35ms
Set Theme "Dracula"

Hide
Type "source scripts/recordings/env.sh" Enter
Sleep 1s
Type "bc_venv && clear" Enter
Sleep 6s
Show
Type "python chat_app.py" Enter
Sleep 4s
Type "python -m json.tool --json-lines runs.jsonl | head -24" Enter
Sleep 4s
Type "clear; briefcase replay explain runs.jsonl" Enter
Sleep 4s
Type "briefcase replay run runs.jsonl --executor cassette --out report.jsonl" Enter
Sleep 4s
Type "briefcase replay diff report.jsonl" Enter
Sleep 4s
Type "clear; python chain_app.py && briefcase verify evidence.db" Enter
Sleep 5s
Type "python tamper.py && briefcase verify evidence.db" Enter
Sleep 6s

# Run from the repo root: WHEEL=/path/to/briefcase_ai-*.whl vhs scripts/recordings/launch.tape
Output "/Users/aansh/briefcase-ws/recordings/sdk/launch.mp4"
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
Type "clear" Enter
Show
Type "uv venv -q --python 3.12 .venv && source .venv/bin/activate" Enter
Sleep 2s
Type "uv pip install $WHEEL" Enter
Sleep 5s
Type "clear; briefcase --version" Enter
Sleep 3s
Type "clear; briefcase" Enter
Sleep 6s
Type "4" Enter
Sleep 3s

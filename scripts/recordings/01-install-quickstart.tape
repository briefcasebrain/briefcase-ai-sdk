# Run from the repo root: WHEEL=/path/to/briefcase_ai-*.whl vhs scripts/recordings/01-install-quickstart.tape
Output "/Users/aansh/briefcase-ws/recordings/sdk/01-install-quickstart.mp4"
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
Type "clear; cat quickstart.py" Enter
Sleep 4s
Type "python quickstart.py" Enter
Sleep 4s
Type "python -c 'import briefcase; print(briefcase.__version__)'" Enter
Sleep 2s
Type "briefcase --version" Enter
Sleep 3s

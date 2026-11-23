# Run from the repo root: WHEEL=/path/to/briefcase_ai-*.whl vhs scripts/recordings/02-cli-tour.tape
Output "/Users/aansh/briefcase-ws/recordings/sdk/02-cli-tour.mp4"
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
Type "briefcase --version && briefcase --help | head -32" Enter
Sleep 6s
Type "clear; briefcase status" Enter
Sleep 6s
Type "clear; briefcase features | head -24" Enter
Sleep 5s
Type "clear; briefcase doctor | head -12" Enter
Sleep 5s
Type "clear; briefcase budget set daily --usd 20 && briefcase budget status" Enter
Sleep 4s
Type "clear; briefcase guard test --text 'password=hunter22'" Enter
Sleep 5s
Type "clear; briefcase setup" Enter
Sleep 3s
Type "4" Enter
Sleep 4s

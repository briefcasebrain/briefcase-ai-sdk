# Run from the repo root: WHEEL=/path/to/briefcase_ai-*.whl vhs scripts/recordings/04-engine.tape
Output "/Users/aansh/briefcase-ws/recordings/sdk/04-engine.mp4"
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
Type "briefcase-ai engine --help | head -30" Enter
Sleep 5s
Type "clear; briefcase-ai engine install --code bci_demo --yes; echo exit=$?" Enter
Sleep 5s
Type "echo '{}' > engine-manifest.json; briefcase-ai engine verify engine-manifest.json; echo exit=$?" Enter
Sleep 5s
Type "clear; briefcase-ai engine status; echo exit=$?" Enter
Sleep 4s
Type "briefcase-ai engine up; echo exit=$?" Enter
Sleep 4s
Type "briefcase-ai graph log main; echo exit=$?" Enter
Sleep 4s
Type "briefcase-ai engine doctor; echo exit=$?" Enter
Sleep 5s

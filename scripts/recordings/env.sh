# Sourced by the .tape files. Needs WHEEL (path to a built briefcase_ai wheel).
# Gives the recorded shell a throwaway HOME and working directory, removed when the shell exits.
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
export HOME="$WORK/home" BRIEFCASE_TELEMETRY=0
mkdir -p "$HOME" "$WORK/demo"
export PS1='$ '
cp "$(dirname "${BASH_SOURCE[0]}")"/*.py "$WORK/demo/"
cd "$WORK/demo" || return
bc_venv() {
  uv venv -q --python 3.12 "$WORK/venv" && . "$WORK/venv/bin/activate" && uv pip install -q "$WHEEL" openai
}

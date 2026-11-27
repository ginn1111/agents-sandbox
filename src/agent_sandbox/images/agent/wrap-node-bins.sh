#!/bin/sh
# wrap-node-bins SRC_BIN_DIR OUT_DIR NODE
#
# For every executable in SRC_BIN_DIR whose script starts with a `node` shebang, write a launcher
# in OUT_DIR that runs it on the fixed NODE binary instead of whichever `node` is first on PATH.
# This keeps agents working when a project pins another Node through mise, without changing the
# PATH of the commands the agent itself runs. Native or non-node executables are left out of
# OUT_DIR; they are still found through SRC_BIN_DIR, which follows OUT_DIR on PATH.
set -eu
src=$1 out=$2 node=$3
mkdir -p "$out"
for f in "$src"/*; do
  [ -e "$f" ] || continue
  real=$(readlink -f "$f")
  [ -f "$real" ] || continue
  first=$(head -n 1 "$real" 2>/dev/null | tr -d '\r' || true)
  case "$first" in
    '#!'*node*)
      name=$(basename "$f")
      printf '#!/bin/sh\nexec "%s" "%s" "$@"\n' "$node" "$real" > "$out/$name"
      chmod 0755 "$out/$name"
      ;;
  esac
done

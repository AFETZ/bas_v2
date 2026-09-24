#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BASE_REF="${BASE_REF:-HEAD}"
cd "$ROOT_DIR"

declare -A selected=()
while IFS= read -r path; do
  [[ -n "$path" ]] && selected["$path"]=1
done < <(
  {
    git diff --name-only --diff-filter=ACMR "$BASE_REF" --
    git ls-files --others --exclude-standard
  } | sort -u
)

if ((${#selected[@]} == 0)); then
  printf 'No changed paths relative to %s.\n' "$BASE_REF"
  exit 0
fi

mapfile -t changed_paths < <(printf '%s\n' "${!selected[@]}" | sort)
markdown_files=()
launch_check=0
position_check=0
sionna_check=0
ns3_check=0
bridge_check=0
hitl_check=0

for path in "${changed_paths[@]}"; do
  [[ -e "$path" ]] || continue
  if [[ "$path" == archive/acceptance_v3/* ]]; then
    printf 'SKIP archived legacy path %s\n' "$path"
    continue
  fi

  case "$path" in
    *.sh)
      printf 'CHECK bash syntax %s\n' "$path"
      bash -n "$path"
      ;;
    *.py)
      printf 'CHECK Python compile %s\n' "$path"
      python3 -m compileall -q "$path"
      ;;
    *.toml)
      printf 'CHECK TOML parse %s\n' "$path"
      python3 - "$path" <<'PY'
import sys
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
with open(sys.argv[1], "rb") as source:
    tomllib.load(source)
PY
      ;;
    *.json)
      printf 'CHECK JSON parse %s\n' "$path"
      python3 - "$path" <<'PY'
import json
import sys
with open(sys.argv[1], "r", encoding="utf-8") as source:
    json.load(source)
PY
      ;;
    *.yaml|*.yml)
      printf 'CHECK YAML parse %s\n' "$path"
      python3 - "$path" <<'PY'
import sys
import yaml
with open(sys.argv[1], "r", encoding="utf-8") as source:
    yaml.safe_load(source)
PY
      ;;
    *.md)
      markdown_files+=("$path")
      ;;
  esac

  case "$path" in
    src/multiagent_simulation/launch/*|network/config/scenario_5uav.yaml)
      launch_check=1
      ;;
    network/position_tracker/*)
      position_check=1
      ;;
    network/radio_provider/*)
      sionna_check=1
      ;;
    network/ns3/*|network/config/radio_*.yaml)
      ns3_check=1
      ;;
    network/bridge/*|network/config/endpoints.yaml)
      bridge_check=1
      ;;
    network/hitl/*)
      hitl_check=1
      ;;
  esac
done

if ((${#markdown_files[@]} > 0)); then
  printf 'CHECK local Markdown references (%d file(s))\n' "${#markdown_files[@]}"
  python3 - "${markdown_files[@]}" <<'PY'
import pathlib
import re
import sys
from urllib.parse import unquote, urlparse

link_re = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
errors = []
for name in sys.argv[1:]:
    source = pathlib.Path(name)
    text = source.read_text(encoding="utf-8")
    for raw in link_re.findall(text):
        target = raw.strip()
        if target.startswith("<") and ">" in target:
            target = target[1:target.index(">")]
        else:
            target = target.split(maxsplit=1)[0]
        parsed = urlparse(target)
        if not target or target.startswith("#") or parsed.scheme or parsed.netloc:
            continue
        local = unquote(parsed.path)
        candidate = pathlib.Path(local.lstrip("/")) if local.startswith("/") else source.parent / local
        if not candidate.exists():
            errors.append(f"{name}: missing local reference {target}")
if errors:
    raise SystemExit("\n".join(errors))
PY
fi

if ((launch_check)); then
  ./network/tests/check_five_uav_micro_ros_ports.sh
fi
if ((position_check)); then
  python3 network/position_tracker/tracker.py --from-config-once >/dev/null
fi
if ((sionna_check)); then
  python3 network/tests/check_sionna_provider.py
fi
if ((ns3_check)); then
  ./network/tests/check_ns3_packet_core_config.sh
fi
if ((bridge_check)); then
  python3 network/bridge/priority_udp_bridge.py --self-test
fi
if ((hitl_check)); then
  ./network/tests/test_hitl_loopback.sh
fi

git diff --check "$BASE_REF" --
printf 'Changed-path checks passed for %d path(s).\n' "${#changed_paths[@]}"

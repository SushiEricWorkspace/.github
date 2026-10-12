#!/bin/sh
# OS別入口は引数をそのままPython基盤へ渡す。
client_python=${SUSHIERIC_PYTHON:-python3.12}
if [ "${1-}" = "--python" ]; then
    if [ "$#" -lt 2 ]; then
        printf '%s\n' 'Python 3.12の実行パスが必要です。' >&2
        exit 2
    fi
    client_python=$2
    shift 2
fi
if ! command -v "$client_python" >/dev/null 2>&1; then
    printf '%s\n' 'Python 3.12が必要です。--pythonで64bit Python 3.12の絶対パスを指定してください。' >&2
    exit 2
fi
script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 2
human_display=--human-readable
for argument do
    if [ "$argument" = "--json" ]; then human_display=; fi
done
exec "$client_python" -B "$script_directory/human-server.py" ${human_display:+"$human_display"} "$@"

#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "usage: IMAGE=<image> $0 OUTPUT_DIR" >&2
    exit 2
}

[[ $# -eq 1 && -n "${IMAGE:-}" ]] || usage
dir="$1"
min_bytes=10240
failures=0
rows=()

fail() {
    echo "FAIL: $*" >&2
    failures=$((failures + 1))
}

size_of() {
    stat -c %s "$1"
}

check_size() {
    local file="$1" size
    size="$(size_of "$file")"
    if ((size < min_bytes)); then
        fail "$file is only $size bytes"
    fi
}

count_articles() {
    unzip -Z1 "$1" | grep -cE '^EPUB/phile[0-9]+\.xhtml$' || true
}

check_epub_contents() {
    local epub="$1" entries entry
    entries="$(unzip -Z1 "$epub")" || { fail "$epub is not a readable zip archive"; return; }
    for entry in EPUB/cover.jpg EPUB/contents.xhtml EPUB/nav.xhtml; do
        grep -qxF "$entry" <<<"$entries" || fail "$epub is missing $entry"
    done
}

check_epubcheck() {
    local epub="$1"
    epubcheck --quiet "$epub" || fail "epubcheck reported errors in $epub"
}

check_azw3_title() {
    local azw3="$1" issue="$2" meta
    meta="$(docker run --rm --entrypoint ebook-meta -v "$(realpath "$dir"):/data/output:ro" "$IMAGE" "/data/output/$(basename "$azw3")")" \
        || { fail "ebook-meta could not read $azw3"; return; }
    grep -qE "^Title +: Phrack #${issue}\$" <<<"$meta" || fail "$azw3 does not have title 'Phrack #$issue'"
}

shopt -s nullglob
epubs=("$dir"/phrack*.epub)
azw3s=("$dir"/phrack*.azw3)

((${#epubs[@]} > 0)) || { echo "FAIL: no epub files in $dir" >&2; exit 1; }

for azw3 in "${azw3s[@]}"; do
    [[ -f "${azw3%.azw3}.epub" ]] || fail "$azw3 has no matching epub"
done

for epub in "${epubs[@]}"; do
    name="$(basename "$epub" .epub)"
    issue="${name#phrack}"
    azw3="${epub%.epub}.azw3"
    articles="$(count_articles "$epub")"

    check_size "$epub"
    check_epub_contents "$epub"
    ((articles > 0)) || fail "$epub has no articles"
    check_epubcheck "$epub"

    if [[ -f "$azw3" ]]; then
        check_size "$azw3"
        check_azw3_title "$azw3" "$issue"
        azw3_size="$(size_of "$azw3")"
    else
        fail "$epub has no matching azw3"
        azw3_size=missing
    fi

    rows+=("$issue|$articles|$(size_of "$epub")|$azw3_size")
done

if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    {
        echo '| Issue | Articles | epub bytes | azw3 bytes |'
        echo '|------:|---------:|-----------:|-----------:|'
        printf '%s\n' "${rows[@]}" | sort -t'|' -k1,1n | awk -F'|' '{ printf "| %s | %s | %s | %s |\n", $1, $2, $3, $4 }'
        echo
        echo "Checked ${#epubs[@]} issue(s), $failures failure(s)."
    } >>"$GITHUB_STEP_SUMMARY"
fi

if ((failures > 0)); then
    echo "$failures check(s) failed" >&2
    exit 1
fi
echo "All checks passed for ${#epubs[@]} issue(s)"

#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "usage: GITHUB_SHA=<sha> [REPLACE=true] $0 OUTPUT_DIR" >&2
    exit 2
}

[[ $# -eq 1 && -n "${GITHUB_SHA:-}" ]] || usage
dir="$1"
replace="${REPLACE:-false}"

shopt -s nullglob
mapfile -t issues < <(
    for epub in "$dir"/phrack*.epub; do
        name="$(basename "$epub" .epub)"
        echo "${name#phrack}"
    done | sort -n
)
((${#issues[@]} > 0)) || { echo "no epub files in $dir" >&2; exit 1; }

newest="$(gh release list --limit 1000 --json tagName --jq '.[].tagName' \
    | sed -n 's/^phrack-\([0-9]\+\)$/\1/p' \
    | cat - <(printf '%s\n' "${issues[@]}") \
    | sort -n | tail -n 1)"

notes() {
    printf 'Phrack issue %s: https://phrack.org/issues/%s/\n\nBuilt from %s.\n' "$1" "$1" "$GITHUB_SHA"
}

for issue in "${issues[@]}"; do
    tag="phrack-$issue"
    assets=("$dir/phrack$issue.epub" "$dir/phrack$issue.azw3")
    latest=false
    [[ "$issue" == "$newest" ]] && latest=true

    if gh release view "$tag" >/dev/null 2>&1; then
        if [[ "$replace" != true ]]; then
            echo "$tag: release exists, skipping"
            continue
        fi
        gh release upload "$tag" "${assets[@]}" --clobber
        gh release edit "$tag" --notes "$(notes "$issue")" --latest="$latest"
        echo "$tag: assets replaced"
    else
        gh release create "$tag" "${assets[@]}" \
            --target "$GITHUB_SHA" --title "Phrack #$issue" --notes "$(notes "$issue")" --latest="$latest"
        echo "$tag: release created"
    fi
done

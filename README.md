# phrack2ebook
Convert Phrack issues to e-reader friendly formats

Each issue becomes an EPUB with one chapter per article (titled and credited from the article metadata), a navigable table of contents including article sections, a contents page, a generated cover and series metadata.

## Usage

Run with [uv](https://docs.astral.sh/uv/), which resolves the dependencies declared in the script:

```
uv run phrack2ebook.py -n 72
```

```
usage: phrack2ebook.py [-h] [-n ISSUE | -a] [-f FORMAT] [-o OUTPUT_DIR] [--cache-dir CACHE_DIR]

Download Phrack issues and convert them to ebooks

options:
  -h, --help            show this help message and exit
  -n, --issue ISSUE     Issue(s) to convert, e.g. 72, 60-73 or 1,5,70-72 (default: latest)
  -a, --all             Convert every issue
  -f, --format FORMAT   Output format; non-epub formats need calibre (default: epub)
  -o, --output-dir OUTPUT_DIR
                        Output directory (default: output)
  --cache-dir CACHE_DIR
                        Download cache directory
```

Downloaded issues are cached in `$XDG_CACHE_HOME/phrack2ebook` (or `~/.cache/phrack2ebook`), so re-runs do not download again.

EPUB works with Kobo and other readers, and with Kindle via Send to Kindle. Other formats (`mobi`, `azw3`, `pdf`, ...) are produced from the EPUB with calibre's `ebook-convert`, which must be on `PATH`.

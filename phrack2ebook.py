# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "requests>=2.31",
#     "ebooklib>=0.18",
#     "pillow>=10.1",
# ]
# ///
import argparse
import html
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin
from typing import NamedTuple

import requests
from ebooklib import epub
from PIL import Image, ImageDraw, ImageFont

ARCHIVE_URL = 'https://archives.phrack.org/tgz/'
SITE_URL = 'https://phrack.org/'
CANONICAL_LINK = re.compile(r'<link rel="canonical" href="([^"]+)"')
DATE_PUBLISHED = re.compile(r'"datePublished":"(\d{4}-\d{2}-\d{2})"')
CONTROL_CHARS = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')
SECTION_LINE = re.compile(r'^\s{0,20}-{1,4}\[(?!\[)\s*(.+?)\s*$')
SECTION_NOISE = re.compile(r'\s*(?:-+\[|\]-+|\]?=+-*|\]\s*$)\s*')
SECTION_REJECT = re.compile(r'@[^\]]*\][$#%]|\]-*>')
SECTION_CLOSED = re.compile(r'\]\s*[-=]*\s*$')
WRAPPED_HEADING_MIN_LENGTH = 60
MAX_HEADING_CONTINUATIONS = 2
URL = re.compile(r'https?://[^\s<>"\'`{}|\\^\[\]]+')
LINKABLE_URL = re.compile(r'https?://([\w-]+(?:\.[\w-]+)+)')
RESERVED_TLDS = {'local', 'test', 'example', 'invalid', 'localhost', 'internal', 'lan'}
RESERVED_DOMAINS = ('example.com', 'example.org', 'example.net')

STYLESHEET = '''
body { margin: 0 2%; }
a { color: inherit; }
.cover { margin: 0; padding: 0; text-align: center; }
.cover img { display: block; margin: 0 auto; max-width: 100%; max-height: 100vh; }
h1 { font-family: sans-serif; font-size: 1.4em; margin: 0.5em 0 0.2em; page-break-before: always; }
.byline { font-family: sans-serif; font-style: italic; margin: 0 0 1.5em; }
pre .heading { font-weight: bold; }
pre {
    white-space: pre-wrap;
    word-wrap: break-word;
    overflow-wrap: break-word;
    font-family: monospace;
    font-size: 0.7em;
    line-height: 1.2;
    margin: 0;
}
ol.contents { padding-left: 1.5em; font-family: sans-serif; }
ol.contents li { margin: 0 0 0.6em; }
ol.contents a { text-decoration: none; }
ol.contents .author { display: block; font-size: 0.85em; font-style: italic; }
'''


class Heading(NamedTuple):
    start: int
    end: int
    label: str


@dataclass
class Article:
    number: int
    title: str
    author: str
    lines: list[str]
    sections: list[Heading] = field(default_factory=list)

    @property
    def file_name(self):
        return f'phile{self.number:02d}.xhtml'


def latest_issue():
    response = requests.get(ARCHIVE_URL, timeout=30)
    response.raise_for_status()
    return max(int(n) for n in re.findall(r'phrack(\d+)\.tar\.gz', response.text))


def default_cache_dir():
    base = os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache'
    return Path(base) / 'phrack2ebook'


def fetch_issue(issue, cache_dir):
    target = cache_dir / f'phrack{issue}.tar.gz'
    if target.exists():
        return target
    cache_dir.mkdir(parents=True, exist_ok=True)
    url = f'{ARCHIVE_URL}phrack{issue}.tar.gz'
    print(f'Downloading {url}')
    partial = target.with_name(target.name + '.part')
    with requests.get(url, stream=True, timeout=30) as response:
        response.raise_for_status()
        with open(partial, 'wb') as f:
            for chunk in response.iter_content(chunk_size=1 << 16):
                f.write(chunk)
    partial.rename(target)
    return target


def issue_date(issue):
    try:
        stub = requests.get(f'{SITE_URL}issues/{issue}/1.html', timeout=30)
        stub.raise_for_status()
        canonical = CANONICAL_LINK.search(stub.text)
        page = requests.get(urljoin(stub.url, canonical.group(1)), timeout=30) if canonical else stub
        page.raise_for_status()
    except requests.RequestException as e:
        print(f'Phrack {issue}: release date unavailable ({e})')
        return None
    match = DATE_PUBLISHED.search(page.text)
    if not match:
        print(f'Phrack {issue}: release date not found on {page.url}')
    return match and match.group(1)


def clean_text(text):
    text = text.replace('\r\n', '\n').replace('\r', '\n').expandtabs(8)
    return CONTROL_CHARS.sub('', text)


def split_front_matter(text):
    if not text.startswith('---\n'):
        return {}, text
    end = text.find('\n---\n', 4)
    if end == -1:
        return {}, text
    meta = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(':')
        if sep:
            meta[key.strip().lower()] = value.strip().strip('"\'')
    return meta, text[end + 5:]


def trim_lines(text):
    lines = [line.rstrip() for line in text.split('\n')]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def normalize_label(text):
    text = SECTION_NOISE.sub(' ', text)
    if '[' not in text:
        text = text.replace(']', ' ')
    return ' '.join(text.split()).strip(' -=')


def section_label(line):
    match = SECTION_LINE.match(line)
    if not match or SECTION_REJECT.search(line):
        return None
    label = normalize_label(match.group(1))
    if not re.search(r'\w', label) or len(label) > 80 or label.upper().startswith('END'):
        return None
    return label


def heading_end(lines, start):
    if len(lines[start].strip()) < WRAPPED_HEADING_MIN_LENGTH or SECTION_CLOSED.search(lines[start]):
        return start + 1
    end = start + 1
    while (
        end < len(lines)
        and end - start <= MAX_HEADING_CONTINUATIONS
        and lines[end].strip()
        and not SECTION_LINE.match(lines[end])
    ):
        end += 1
        if end == len(lines) or not lines[end].strip():
            return end
    return start + 1


def find_sections(lines):
    headings = []
    for i, line in enumerate(lines):
        if label := section_label(line):
            end = heading_end(lines, i)
            headings.append(Heading(i, end, normalize_label(' '.join([label, *lines[i + 1:end]]))))
    return headings


def parse_article(number, raw):
    meta, body = split_front_matter(clean_text(raw))
    lines = trim_lines(body)
    return Article(
        number=number,
        title=meta.get('title') or f'Phile {number}',
        author=meta.get('author', ''),
        lines=lines,
        sections=find_sections(lines),
    )


def read_articles(tar_path):
    articles = []
    with tarfile.open(tar_path) as tar:
        for member in tar.getmembers():
            stem = Path(member.name).stem
            if member.isfile() and member.name.endswith('.txt') and stem.isdigit():
                raw = tar.extractfile(member).read().decode('utf-8', errors='replace')
                articles.append(parse_article(int(stem), raw))
    return sorted(articles, key=lambda a: a.number)


def trim_url(url):
    while url and url[-1] in '.,;:!?\'")':
        if url[-1] == ')' and url.count('(') >= url.count(')'):
            break
        url = url[:-1]
    return url


def is_reserved_host(host):
    return host.rsplit('.', 1)[-1] in RESERVED_TLDS or any(
        host == domain or host.endswith('.' + domain) for domain in RESERVED_DOMAINS
    )


def is_linkable(url, rest_of_line):
    match = LINKABLE_URL.match(url)
    if not match or is_reserved_host(match.group(1).lower()):
        return False
    # a trailing '-'/'&' or a shell '\' continuation means the URL was hard-wrapped; linking the fragment would 404
    return not url.endswith(('-', '&')) and rest_of_line.strip() != '\\'


def linkify(line):
    parts, pos = [], 0
    for match in URL.finditer(line):
        url = trim_url(match.group())
        if not is_linkable(url, line[match.start() + len(url):]):
            continue
        parts.append(html.escape(line[pos:match.start()], quote=False))
        parts.append(f'<a href="{html.escape(url)}">{html.escape(url, quote=False)}</a>')
        pos = match.start() + len(url)
    parts.append(html.escape(line[pos:], quote=False))
    return ''.join(parts)


def render_article(article):
    heading_attrs = {}
    for k, heading in enumerate(article.sections):
        heading_attrs[heading.start] = f' id="s{k}"'
        heading_attrs.update(dict.fromkeys(range(heading.start + 1, heading.end), ''))
    body = []
    for i, line in enumerate(article.lines):
        escaped = linkify(line)
        if i in heading_attrs:
            escaped = f'<span class="heading"{heading_attrs[i]}>{escaped}</span>'
        body.append(escaped)
    byline = f'<p class="byline">{html.escape(article.author)}</p>' if article.author else ''
    return f'<h1>{html.escape(article.title)}</h1>{byline}<pre>{chr(10).join(body)}</pre>'


def render_contents(articles):
    items = []
    for a in articles:
        author = f'<span class="author">{html.escape(a.author)}</span>' if a.author else ''
        items.append(f'<li><a href="{a.file_name}">{html.escape(a.title)}</a>{author}</li>')
    return f'<h1>Contents</h1><ol class="contents">{"".join(items)}</ol>'


def fitted_font(draw, text, size, max_width):
    font = ImageFont.load_default(size=size)
    while draw.textlength(text, font=font) > max_width and size > 10:
        size -= 10
        font = ImageFont.load_default(size=size)
    return font


def draw_centered(draw, y, text, font, fill, width):
    x = (width - draw.textlength(text, font=font)) / 2
    draw.text((x, y), text, font=font, fill=fill)


def make_cover(issue):
    width, height = 1600, 2560
    fg, dim = (80, 255, 120), (40, 140, 70)
    image = Image.new('RGB', (width, height), (8, 10, 8))
    draw = ImageDraw.Draw(image)
    draw.rectangle((60, 60, width - 60, height - 60), outline=dim, width=6)
    draw_centered(draw, 260, '==Phrack Inc.==', fitted_font(draw, '==Phrack Inc.==', 90, 1200), dim, width)
    draw_centered(draw, 900, 'PHRACK', fitted_font(draw, 'PHRACK', 380, 1320), fg, width)
    draw_centered(draw, 1400, f'Issue #{issue}', ImageFont.load_default(size=160), fg, width)
    draw_centered(draw, 1620, f'0x{issue:02x}', ImageFont.load_default(size=110), dim, width)
    draw_centered(draw, 2300, 'by hackers, for hackers', ImageFont.load_default(size=70), dim, width)
    buffer = io.BytesIO()
    image.save(buffer, format='JPEG', quality=90)
    return buffer.getvalue()


def add_series_metadata(book, issue):
    book.add_metadata(None, 'meta', '', {'name': 'calibre:series', 'content': 'Phrack'})
    book.add_metadata(None, 'meta', '', {'name': 'calibre:series_index', 'content': str(issue)})
    book.add_metadata(None, 'meta', 'Phrack', {'property': 'belongs-to-collection', 'id': 'series'})
    book.add_metadata(None, 'meta', 'series', {'refines': '#series', 'property': 'collection-type'})
    book.add_metadata(None, 'meta', str(issue), {'refines': '#series', 'property': 'group-position'})


def html_item(title, file_name, content, css):
    item = epub.EpubHtml(title=title, file_name=file_name, lang='en')
    item.content = content
    item.add_item(css)
    return item


def toc_entry(article):
    if not article.sections:
        return epub.Link(article.file_name, article.title, f'p{article.number}')
    links = [
        epub.Link(f'{article.file_name}#s{k}', heading.label, f'p{article.number}s{k}')
        for k, heading in enumerate(article.sections)
    ]
    return (epub.Section(article.title, article.file_name), links)


def build_epub(issue, articles, path, date=None):
    book = epub.EpubBook()
    book.set_identifier(f'phrack-{issue}')
    book.set_title(f'Phrack #{issue}')
    book.set_language('en')
    book.add_author('Phrack')
    if date:
        book.add_metadata('DC', 'date', date)
    add_series_metadata(book, issue)
    book.set_cover('cover.jpg', make_cover(issue), create_page=False)

    css = epub.EpubItem(uid='style', file_name='style.css', media_type='text/css', content=STYLESHEET)
    book.add_item(css)

    cover = html_item('Cover', 'cover.xhtml', f'<div class="cover"><img src="cover.jpg" alt="Phrack #{issue}"/></div>', css)
    book.add_item(cover)

    contents = html_item('Contents', 'contents.xhtml', render_contents(articles), css)
    book.add_item(contents)
    chapters = []
    for article in articles:
        chapter = html_item(article.title, article.file_name, render_article(article), css)
        book.add_item(chapter)
        chapters.append(chapter)

    book.toc = [epub.Link('contents.xhtml', 'Contents', 'contents')] + [toc_entry(a) for a in articles]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = [cover, contents, *chapters]
    epub.write_epub(str(path), book)


def convert(epub_path, out_path):
    subprocess.run(['ebook-convert', str(epub_path), str(out_path)], check=True, stdout=subprocess.DEVNULL)


def handle_issue(issue, fmt, output_dir, cache_dir, force):
    out_path = output_dir / f'phrack{issue}.{fmt}'
    if out_path.exists() and not force:
        print(f'Phrack {issue}: {out_path} exists, skipping')
        return
    articles = read_articles(fetch_issue(issue, cache_dir))
    if not articles:
        raise RuntimeError(f'no articles found in Phrack {issue}')
    date = issue_date(issue)
    output_dir.mkdir(parents=True, exist_ok=True)
    if fmt == 'epub':
        build_epub(issue, articles, out_path, date)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / f'phrack{issue}.epub'
            build_epub(issue, articles, epub_path, date)
            convert(epub_path, out_path)
    print(f'Phrack {issue}: {len(articles)} articles -> {out_path}')


def parse_issue_spec(spec):
    issues = set()
    for part in spec.split(','):
        start, sep, end = part.strip().partition('-')
        if not start.isdigit() or (sep and not end.isdigit()):
            raise ValueError(f'invalid issue spec: {part!r}')
        first, last = int(start), int(end) if sep else int(start)
        if first < 1 or last < first:
            raise ValueError(f'invalid issue range: {part!r}')
        issues.update(range(first, last + 1))
    return sorted(issues)


def parse_args():
    parser = argparse.ArgumentParser(description='Download Phrack issues and convert them to ebooks')
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('-n', '--issue', help='Issue(s) to convert, e.g. 72, 60-73 or 1,5,70-72 (default: latest)')
    selection.add_argument('-a', '--all', action='store_true', help='Convert every issue')
    parser.add_argument('-f', '--format', default='epub', help='Output format; non-epub formats need calibre (default: epub)')
    parser.add_argument('-o', '--output-dir', type=Path, default=Path('output'), help='Output directory (default: output)')
    parser.add_argument('--cache-dir', type=Path, default=default_cache_dir(), help='Download cache directory')
    parser.add_argument('--force', action='store_true', help='Rebuild issues whose output file already exists')
    args = parser.parse_args()
    if args.issue:
        try:
            args.issues = parse_issue_spec(args.issue)
        except ValueError as e:
            parser.error(str(e))
    return args


def main():
    args = parse_args()
    fmt = args.format.lower().lstrip('.')
    if fmt != 'epub' and not shutil.which('ebook-convert'):
        sys.exit(f'error: ebook-convert (calibre) is required to produce {fmt} output')
    try:
        if args.all:
            args.issues = list(range(1, latest_issue() + 1))
        elif not args.issue:
            args.issues = [latest_issue()]
        for issue in args.issues:
            handle_issue(issue, fmt, args.output_dir, args.cache_dir, args.force)
    except (requests.RequestException, RuntimeError, subprocess.CalledProcessError, tarfile.TarError) as e:
        sys.exit(f'error: {e}')


if __name__ == '__main__':
    main()

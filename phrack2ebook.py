# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "requests>=2.31",
#     "ebooklib>=0.18",
#     "pillow>=10.1",
# ]
# ///
import argparse
import base64
import binascii
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
OUTPUT_FORMATS = (
    'epub', 'azw3', 'docx', 'fb2', 'htmlz', 'lit', 'lrf', 'mobi', 'oeb', 'pdb',
    'pdf', 'pml', 'rb', 'rtf', 'snb', 'tcr', 'txt', 'txtz', 'zip',
)
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
ENCODED_BEGIN = re.compile(r'^begin(-base64)? [0-7]{3,4} (\S.*)$')
BASE64_LINE = re.compile(r'^[A-Za-z0-9+/]{40,}={0,2}$')
BASE64_MIXED = re.compile(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)')
BASE64_TAIL = re.compile(r'^[A-Za-z0-9+/]+={0,2}$')
MIN_BASE64_RUN = 10
MIN_BASE64_MIXED_RATIO = 0.25
SNIPPET_LINES = 3
UU_FULL_LINE = re.compile(r'^M[\x20-\x60]{40,61}[\x61-\x7e]?$')
UU_LINE = re.compile(r'^[\x20-\x60]+[\x61-\x7e]?$')
MIN_UU_RUN = 10
PRINTABLE_BYTES = bytes([9, 10, 13, *range(32, 127)])
MIN_TEXT_RATIO = 0.95
IMAGE_SUFFIXES = {'.gif', '.jpg', '.jpeg', '.png', '.pcx', '.bmp'}
EPUB_IMAGE_TYPES = {'GIF': ('image/gif', 'gif'), 'JPEG': ('image/jpeg', 'jpg'), 'PNG': ('image/png', 'png')}
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
pre .omitted { font-style: italic; }
.figure { margin: 1em 0; text-align: center; page-break-inside: avoid; }
.figure img { max-width: 100%; max-height: 90vh; }
.caption { font-family: sans-serif; font-size: 0.8em; font-style: italic; margin: 0.3em 0 0; }
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


class EncodedBlock(NamedTuple):
    start: int
    end: int
    encoding: str
    name: str | None = None
    terminated: bool = True

    @property
    def description(self):
        return f'{self.encoding} {self.name or "data"}'

    @property
    def continues_file(self):
        return self.encoding == 'uuencoded' and self.name is None


class EmbeddedImage(NamedTuple):
    file_name: str
    media_type: str
    content: bytes
    caption: str


@dataclass
class Article:
    number: int
    title: str
    author: str
    lines: list[str]
    sections: list[Heading] = field(default_factory=list)
    omitted: set[int] = field(default_factory=set)
    images: dict[int, EmbeddedImage] = field(default_factory=dict)

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


def encoded_block_end(lines, first, terminator, line_pattern=None):
    end = first
    while end < len(lines):
        line = lines[end]
        if line == terminator:
            return end + 1, True
        if not line:
            # uuencode's zero-length line is a lone space, which trim_lines turns into a blank line before 'end'
            return (end + 2, True) if lines[end + 1:end + 2] == [terminator] else (end, False)
        if line_pattern and not line_pattern.match(line):
            return end, False
        end += 1
    return end, False


def base64_run_end(lines, start):
    end = start
    while end < len(lines) and BASE64_LINE.match(lines[end]):
        end += 1
    if end < len(lines) and BASE64_TAIL.match(lines[end]):
        end += 1
    return end


def is_base64_payload(run):
    mixed = sum(1 for line in run if BASE64_MIXED.match(line))
    return len(run) >= MIN_BASE64_RUN and mixed >= MIN_BASE64_MIXED_RATIO * len(run)


def is_uu_run(lines, start):
    return len(lines) - start >= MIN_UU_RUN and all(UU_FULL_LINE.match(line) for line in lines[start:start + MIN_UU_RUN])


def find_encoded_blocks(lines):
    blocks, i, in_pgp = [], 0, False
    while i < len(lines):
        line = lines[i]
        if line.startswith('-----BEGIN PGP'):
            in_pgp = True
        elif line.startswith('-----END PGP'):
            in_pgp = False
        elif match := ENCODED_BEGIN.match(line):
            is_base64 = bool(match.group(1))
            end, terminated = encoded_block_end(lines, i + 1, '====' if is_base64 else 'end')
            encoding = 'base64-encoded' if is_base64 else 'uuencoded'
            blocks.append(EncodedBlock(i, end, encoding, match.group(2), terminated))
            i = end
            continue
        elif is_uu_run(lines, i):
            end, terminated = encoded_block_end(lines, i, 'end', UU_LINE)
            blocks.append(EncodedBlock(i, end, 'uuencoded', None, terminated))
            i = end
            continue
        elif not in_pgp and BASE64_LINE.match(line):
            end = base64_run_end(lines, i)
            if is_base64_payload(lines[i:end]):
                blocks.append(EncodedBlock(i, end, 'base64-encoded'))
            i = end
            continue
        i += 1
    return blocks


def decode_uu_line(line):
    try:
        return binascii.a2b_uu(line)
    except binascii.Error:
        length = (ord(line[0]) - 32) & 63
        width = (length + 2) // 3 * 4
        chars = line[1:1 + width].ljust(width)
        decoded = bytearray()
        for i in range(0, width, 4):
            a, b, c, d = ((ord(ch) - 32) & 63 for ch in chars[i:i + 4])
            decoded += bytes(((a << 2 | b >> 4) & 255, (b << 4 | c >> 2) & 255, (c << 6 | d) & 255))
        return bytes(decoded[:length])


def decode_block(lines, block):
    if block.encoding == 'base64-encoded':
        if block.name is None:
            return None
        try:
            return base64.b64decode(''.join(line for line in lines[block.start + 1:block.end] if line != '===='))
        except (binascii.Error, ValueError):
            return None
    body = lines[block.start + (block.name is not None):block.end]
    return b''.join(decode_uu_line(line) for line in body if line and line != 'end')


def image_payload(name, data):
    if Path(name).suffix.lower() not in IMAGE_SUFFIXES:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.format in EPUB_IMAGE_TYPES:
                return data, *EPUB_IMAGE_TYPES[image.format]
            buffer = io.BytesIO()
            image.save(buffer, format='PNG')
            return buffer.getvalue(), *EPUB_IMAGE_TYPES['PNG']
    except (OSError, SyntaxError, ValueError):
        return None


def text_payload(data):
    if not data or b'\x00' in data:
        return None
    if len(data.translate(None, PRINTABLE_BYTES)) > (1 - MIN_TEXT_RATIO) * len(data):
        return None
    return trim_lines(clean_text(data.decode('utf-8', errors='replace')))


def snippet(lines, block, label=None):
    hidden = block.end - block.start - 2 * SNIPPET_LINES
    if hidden <= 1:
        return lines[block.start:block.end], None
    note = f'... {hidden:,} lines of {label or block.description} omitted ...'
    return [*lines[block.start:block.start + SNIPPET_LINES], note, *lines[block.end - SNIPPET_LINES:block.end]], SNIPPET_LINES


class ParsedArticle(NamedTuple):
    number: int
    meta: dict
    lines: list[str]
    blocks: list[EncodedBlock]


def parse_raw_article(number, raw):
    meta, body = split_front_matter(clean_text(raw))
    lines = trim_lines(body)
    return ParsedArticle(number, meta, lines, find_encoded_blocks(lines))


def stitch_payloads(parsed):
    chunks, continuations, pending = {}, {}, None
    for ai, article in enumerate(parsed):
        for bi, block in enumerate(article.blocks):
            key = (ai, bi)
            data = decode_block(article.lines, block)
            if block.continues_file and pending:
                chunks[pending].append(data)
                owner = parsed[pending[0]].blocks[pending[1]]
                continuations[key] = f'{owner.description} (continued)'
                if block.terminated:
                    pending = None
                continue
            chunks[key] = [data] if data is not None else None
            pending = key if block.encoding == 'uuencoded' and block.name and not block.terminated else None
    payloads = {key: b''.join(parts) for key, parts in chunks.items() if parts is not None}
    return payloads, continuations


def expand_article(parsed, ai, payloads, continuations):
    lines, omitted, images, pos = [], set(), {}, 0
    for bi, block in enumerate(parsed.blocks):
        lines.extend(parsed.lines[pos:block.start])
        pos = block.end
        data = payloads.get((ai, bi))
        if block.name and data is not None and (image := image_payload(block.name, data)):
            content, media_type, suffix = image
            omitted.add(len(lines))
            images[len(lines)] = EmbeddedImage(
                f'images/phile{parsed.number:02d}-{len(images) + 1}.{suffix}', media_type, content, block.name
            )
            lines.append(f'[ image: {block.name} ]')
        elif block.name and data is not None and (text := text_payload(data)):
            omitted.add(len(lines))
            lines.append(f'[ {block.name}, decoded from {block.encoding.removesuffix("-encoded")} ]')
            lines.extend(text)
        else:
            block_lines, note_offset = snippet(parsed.lines, block, continuations.get((ai, bi)))
            if note_offset is not None:
                omitted.add(len(lines) + note_offset)
            lines.extend(block_lines)
    lines.extend(parsed.lines[pos:])
    return lines, omitted, images


def build_articles(raw_articles):
    parsed = [parse_raw_article(number, raw) for number, raw in sorted(raw_articles)]
    payloads, continuations = stitch_payloads(parsed)
    articles = []
    for ai, article in enumerate(parsed):
        lines, omitted, images = expand_article(article, ai, payloads, continuations)
        articles.append(Article(
            number=article.number,
            title=article.meta.get('title') or f'Phile {article.number}',
            author=article.meta.get('author', ''),
            lines=lines,
            sections=find_sections(lines),
            omitted=omitted,
            images=images,
        ))
    return articles


def parse_article(number, raw):
    return build_articles([(number, raw)])[0]


def read_articles(tar_path):
    raw_articles = []
    with tarfile.open(tar_path) as tar:
        for member in tar.getmembers():
            stem = Path(member.name).stem
            if member.isfile() and member.name.endswith('.txt') and stem.isdigit():
                raw_articles.append((int(stem), tar.extractfile(member).read().decode('utf-8', errors='replace')))
    return build_articles(raw_articles)


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
    parts, body = [], []
    for i, line in enumerate(article.lines):
        if i in article.images:
            parts.append(render_pre(body))
            parts.append(render_figure(article.images[i]))
            body = []
            continue
        escaped = linkify(line)
        if i in heading_attrs:
            escaped = f'<span class="heading"{heading_attrs[i]}>{escaped}</span>'
        elif i in article.omitted:
            escaped = f'<span class="omitted">{escaped}</span>'
        body.append(escaped)
    parts.append(render_pre(body))
    byline = f'<p class="byline">{html.escape(article.author)}</p>' if article.author else ''
    return f'<h1>{html.escape(article.title)}</h1>{byline}{"".join(parts)}'


def render_pre(body):
    return f'<pre>{chr(10).join(body)}</pre>' if body else ''


def render_figure(image):
    caption = html.escape(image.caption)
    return (
        f'<div class="figure"><img src="{html.escape(image.file_name)}" alt="{caption}"/>'
        f'<p class="caption">{caption}</p></div>'
    )


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
        for image in article.images.values():
            book.add_item(epub.EpubImage(
                uid=Path(image.file_name).stem, file_name=image.file_name, media_type=image.media_type, content=image.content
            ))
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


def output_format(value):
    return value.lower().lstrip('.')


def parse_args():
    parser = argparse.ArgumentParser(description='Download Phrack issues and convert them to ebooks')
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('-n', '--issue', help='Issue(s) to convert, e.g. 72, 60-73 or 1,5,70-72 (default: latest)')
    selection.add_argument('-a', '--all', action='store_true', help='Convert every issue')
    parser.add_argument(
        '-f', '--format', type=output_format, choices=OUTPUT_FORMATS, default='epub', metavar='FORMAT',
        help=f'Output format: {", ".join(OUTPUT_FORMATS)}; non-epub formats need calibre (default: epub)',
    )
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
    fmt = args.format
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

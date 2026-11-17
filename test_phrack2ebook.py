import pytest

from phrack2ebook import (
    Heading,
    clean_text,
    find_sections,
    linkify,
    output_format,
    parse_article,
    parse_issue_spec,
    render_article,
    section_label,
    split_front_matter,
    trim_url,
)


@pytest.mark.parametrize(
    ('spec', 'expected'),
    [
        ('72', [72]),
        ('60-63', [60, 61, 62, 63]),
        ('1,5,70-72', [1, 5, 70, 71, 72]),
        ('5, 3, 3-4', [3, 4, 5]),
        ('7-7', [7]),
    ],
)
def test_parse_issue_spec(spec, expected):
    assert parse_issue_spec(spec) == expected


@pytest.mark.parametrize('spec', ['abc', '5-3', '0', '3-', '-3', '', '1,,2', '1-x'])
def test_parse_issue_spec_rejects_invalid(spec):
    with pytest.raises(ValueError):
        parse_issue_spec(spec)


@pytest.mark.parametrize(('value', 'expected'), [('epub', 'epub'), ('MOBI', 'mobi'), ('.azw3', 'azw3')])
def test_output_format_normalizes(value, expected):
    assert output_format(value) == expected


def test_split_front_matter():
    meta, body = split_front_matter('---\ntitle: Linenoise\nauthor: Phrack Staff\n---\nbody text\n')
    assert meta == {'title': 'Linenoise', 'author': 'Phrack Staff'}
    assert body == 'body text\n'


def test_split_front_matter_keeps_colons_and_strips_quotes():
    meta, _ = split_front_matter('---\ntitle: "Desync the Planet: Rsync RCE"\nauthor: \'Simon\'\n---\n')
    assert meta == {'title': 'Desync the Planet: Rsync RCE', 'author': 'Simon'}


@pytest.mark.parametrize('text', ['no front matter\n', '---\ntitle: unterminated\nbody\n'])
def test_split_front_matter_without_block(text):
    assert split_front_matter(text) == ({}, text)


def test_clean_text_normalizes_newlines_tabs_and_control_chars():
    assert clean_text('a\r\nb\rc\td\x00\x1b[0m\x7f') == 'a\nb\nc       d[0m'


def test_parse_article_falls_back_to_phile_number():
    article = parse_article(4, '\n\nplain text\n\n')
    assert (article.title, article.author, article.lines) == ('Phile 4', '', ['plain text'])


@pytest.mark.parametrize(
    ('line', 'label'),
    [
        ('--[ Hacker Evolution ', 'Hacker Evolution'),
        ('---[ 0 - Introduction', '0 - Introduction'),
        ('---[ 1 ---[   THE HISTORY   ]---', '1 THE HISTORY'),
        ('---[ 4.1.1 ---[   FASTBIN METHOD   ]---', '4.1.1 FASTBIN METHOD'),
        ('-[0x01] :: introduction ----------------------------------------------', '0x01 :: introduction'),
        ('--[6 - Description of several SMB packets', '6 - Description of several SMB packets'),
        ('--[ 2. How is CSPP controlling US education network ]--', '2. How is CSPP controlling US education network'),
        ('                --[ Introduction ]--', 'Introduction'),
        ('--[ 14.4 - Structural Symmetry Bypass: CVE-2022-23222 [9] (OR_NULL)', '14.4 - Structural Symmetry Bypass: CVE-2022-23222 [9] (OR_NULL)'),
        ('--[ 4.3 - [BONUS] Stealing your location out of Apple Maps', '4.3 - [BONUS] Stealing your location out of Apple Maps'),
        ('----[ > About Me', '> About Me'),
        ('--[ √-1. Index', '√-1. Index'),
        ('---[ INDEX', 'INDEX'),
    ],
)
def test_section_label_accepts_headings(line, label):
    assert section_label(line) == label


@pytest.mark.parametrize(
    'line',
    [
        '-[nemo@fry:~/code/unipara]$ ./host',
        '        -[nemo@gir:~]$ gdb ./mtst1',
        '-[root@megatron:/Applications/F-Script Anywhere.app/Contents/MacOS]$',
        '-[root@fry:/chroot]# touch file_outside_chroot',
        '                --[PING]->',
        '        --[flags=SYN, SEQ=5000]-->              [server picks an ISN]',
        '                                  -[(%esp,1) itemWithTarget:andAction:]',
        '---[ END INDEX',
        '--[[ lua block comment',
        '|=---=[ Specs',
        '--[ ' + 'x' * 81,
        '--[ ]--',
        'plain text',
    ],
)
def test_section_label_rejects_non_headings(line):
    assert section_label(line) is None


def test_find_sections_joins_wrapped_heading_ending_at_blank_line():
    lines = [
        ' --[ 5.5 - Improper SMM Lock Configuration Enables Unauthorized System',
        'Management Mode (SMM) Modification',
        '- CVE-2025-25738',
        ' ',
        'The UEFI menu shows SMM Lock = Disabled.',
    ]
    assert find_sections(lines) == [
        Heading(0, 3, '5.5 - Improper SMM Lock Configuration Enables Unauthorized System '
                      'Management Mode (SMM) Modification - CVE-2025-25738'),
    ]


def test_find_sections_joins_continuation_with_closing_marker():
    lines = [
        '--[ 7.1 : Extended exhibits: five more proof assistants and a fourth',
        '          SMT oracle ]-----------------------------------------------------',
        '',
    ]
    assert find_sections(lines) == [Heading(0, 2, '7.1 : Extended exhibits: five more proof assistants and a fourth SMT oracle')]


def test_find_sections_does_not_join_without_trailing_blank_line():
    lines = [
        '--[ 14.4 - Structural Symmetry Bypass: CVE-2022-23222 [9] (OR_NULL)',
        'Date: January 2022',
        'Type: Privilege Escalation',
        'Fixed in: kernel 5.16.11',
        '',
    ]
    assert find_sections(lines) == [Heading(0, 1, '14.4 - Structural Symmetry Bypass: CVE-2022-23222 [9] (OR_NULL)')]


@pytest.mark.parametrize(
    'lines',
    [
        ['--[ 2 - The various Netfilter hooks and their uses', 'body text follows directly', ''],
        ['--[ 2. How is CSPP controlling US education network and its schools ]--', '      by dahut', ''],
    ],
)
def test_find_sections_does_not_join_short_or_closed_headings(lines):
    assert [(h.start, h.end) for h in find_sections(lines)] == [(0, 1)]


def test_find_sections_does_not_join_into_next_heading():
    lines = [
        '--[ 2 - The various Netfilter hooks and their uses in the Linux kernel stack',
        "----[ 2.1 - The Linux kernel's handling of packets",
        '',
    ]
    assert [(h.start, h.end) for h in find_sections(lines)] == [(0, 1), (1, 2)]


@pytest.mark.parametrize(
    ('url', 'expected'),
    [
        ('http://www.2600.com.', 'http://www.2600.com'),
        ('http://www.ibio.no)', 'http://www.ibio.no'),
        ('https://en.wikipedia.org/wiki/APT_(software)', 'https://en.wikipedia.org/wiki/APT_(software)'),
        ('https://en.wikipedia.org/wiki/APT_(software)).', 'https://en.wikipedia.org/wiki/APT_(software)'),
        ('http://www.defcon.org,', 'http://www.defcon.org'),
    ],
)
def test_trim_url(url, expected):
    assert trim_url(url) == expected


def test_linkify_wraps_url_and_escapes():
    assert linkify('see <https://a.org/?x=1&y=2>.') == (
        'see &lt;<a href="https://a.org/?x=1&amp;y=2">https://a.org/?x=1&amp;y=2</a>&gt;.'
    )


def test_linkify_strips_trailing_punctuation():
    assert linkify('(http://www.ibio.no)') == '(<a href="http://www.ibio.no">http://www.ibio.no</a>)'


@pytest.mark.parametrize(
    'line',
    [
        'https://www.synacktiv.com/publications/php-filter-chains-',
        'https://mp.weixin.qq.com/s?__biz=MzIxNjkwODg4OQ==&mid=2247483837&',
        '$ curl "http://evil.org/index.php?model=AAAA \\',
        '$ curl "http://orange.local/index.php?model=%p-%p-%p"',
        'http://www.example.com/',
        'http://sub.example.org/x',
        'http://localhost:8080/',
        'http://intranet.lan/',
        'just http:// alone',
    ],
)
def test_linkify_skips_unlinkable_urls(line):
    assert '<a ' not in linkify(line)


def test_render_article_marks_headings_with_anchor_and_escapes():
    article = parse_article(
        5,
        '---\ntitle: A & B\nauthor: <me>\n---\n'
        '--[ 5.5 - Improper SMM Lock Configuration Enables Unauthorized System\n'
        'Management Mode (SMM) Modification\n'
        '\n'
        'if (a < b) return;\n',
    )
    rendered = render_article(article)
    assert rendered.startswith('<h1>A &amp; B</h1><p class="byline">&lt;me&gt;</p><pre>')
    assert '<span class="heading" id="s0">--[ 5.5 - Improper SMM' in rendered
    assert '<span class="heading">Management Mode (SMM) Modification</span>' in rendered
    assert 'if (a &lt; b) return;' in rendered

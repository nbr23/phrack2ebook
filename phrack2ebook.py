import glob
import subprocess
import os
import requests
import re
import sys
import shutil
import html

def find_latest_issue():
    issueRegex = re.compile(r'phrack(\d+).tar.gz')
    issues_list = requests.get('https://archives.phrack.org/tgz/')

    return issueRegex.findall(issues_list.text).pop()

def download_phrack_issue(issue_number):
    url = f'https://archives.phrack.org/tgz/phrack{issue_number}.tar.gz'
    print(f'Downloading Phrack issue {issue_number} from {url}')
    os.makedirs(f'phrack{issue_number}', exist_ok=True)
    output = f'phrack{issue_number}/phrack{issue_number}.tar.gz'
    subprocess.run(['curl', '-qo', output, url])
    print(f'Extracting {output}')
    subprocess.run(['tar', '-xf', output, '-C', f'phrack{issue_number}'])
    print(f'Cleaning up {output}')
    os.remove(output)

def txt_to_html(input_directory, title, filename):
    txt_files = sorted(glob.glob(os.path.join(input_directory, '*.txt')), 
                      key=lambda x: int(os.path.splitext(os.path.basename(x))[0]))
    
    html_content = [
        "<!DOCTYPE html>",
        "<html>",
        "<head>",
        "<title>{}</title>".format(title),
        "</head>",
        "<body>"
    ]
    
    for i, txt_file in enumerate(txt_files, 1):
        with open(txt_file, 'r', encoding='utf-8') as f:
            content = html.escape(f.read())
            content = '<p>' + '</p><p>'.join(content.split('\n')) + '</p>'
            html_content.extend([
                f"<h1>Chapter {i}</h1>",
                "<pre>",  # Use <pre> tag to preserve formatting and use monospace font
                content,
                "</pre>"
            ])
    
    html_content.append("</body></html>")
    with open(filename, 'w', encoding='utf-8') as f:
        f.write('\n'.join(html_content))

def html_to_mobi(html_input, output_mobi, title="My Book", author="Author Name"):
    subprocess.run([
        'ebook-convert',
        html_input,
        output_mobi,
        '--title', title,
        '--authors', author,
        '--chapter', '//h1',  # Use h1 tags as chapter markers
        '--page-breaks-before', '//h1',  # Add page breaks before chapters
        '--change-justification', 'left',  # Left align text
        '--base-font-size', '12',
        '--font-size-mapping', '12,12,12,12,12,12,12',  # Keep font sizes consistent
    ])

def main():
    if len(sys.argv) > 1:
        issue_number = sys.argv[1]
    else:
        issue_number = find_latest_issue()

    download_phrack_issue(issue_number)
    os.makedirs('output', exist_ok=True)

    txt_to_html(
        input_directory=f'phrack{issue_number}',
        title=f'Phrack {issue_number}',
        filename=f'output/phrack{issue_number}.html'
    )

    html_to_mobi(
        output_mobi=f'output/phrack{issue_number}.mobi',
        title=f'Phrack {issue_number}',
        author='Phrack',
        html_input=f'output/phrack{issue_number}.html'
    )

    os.remove(f'output/phrack{issue_number}.html')
    shutil.rmtree(f'phrack{issue_number}', ignore_errors=True)


if __name__ == '__main__':
    main()

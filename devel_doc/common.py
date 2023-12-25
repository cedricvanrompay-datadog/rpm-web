from progressbar import ProgressBar, Bar, Percentage
import json
import os
import re
import subprocess
import sys


def shell(cmd, capture=True, split=False):
    """Run a command in a shell and return its standard output."""
    out = subprocess.run(cmd, capture_output=capture, shell=True, text=True)
    if capture:
        sys.stderr.write(out.stderr)
        out = out.stdout.strip('\n')
        if out and split:
            return out.split('\n')
        return out
    return out

def backports(range):
    """Return the original commit hashes backported from a git range."""
    out = []
    log = shell('git rev-list --pretty="format:%b" {}'.format(range))
    for r in BACKPORT_RE:
        out.extend(re.findall(r, log))
    return out

def progressbar(sequence, label='', hide=False):
    """Show a simple progress bar while yielding elements from a sequence."""

    if hide or not sys.stdout.isatty():
        bar = None
    else:
        bar = SimpleProgressBar(max_value=len(sequence) + 1, label=label)
        bar.start()
        bar.update(1, force=True)

    for i, e in enumerate(sequence):
        yield e
        if bar is None:
            continue
        bar.update(i + 1)

    if bar is None:
        return

    bar.finish()


class KeyType:
    """Git config's entry types."""

    SIMPLE      = 0,
    LIST        = 1,
    SET         = 2,
    MULTILINE   = 3,
    MAP         = 4,

class GitConfig(object):
    """A wrapper for git configuration."""

    def __init__(self, section):
        self.section = section

    def get(self, key, type=KeyType.SIMPLE, default=None):
        if type == KeyType.SIMPLE:
            cmd = '--get'
            sep = None
        if type in (KeyType.LIST, KeyType.SET):
            cmd = '--get'
            sep = ','
        elif type == KeyType.MULTILINE:
            cmd = '--get-all'
            sep = '\n'
        elif type == KeyType.MAP:
            cmd = '--get-regexp'
            sep = '\n'
        out = shell('git config {} {}.{}'.format(cmd, self.section, key))
        if not out:
            return default
        if sep is not None:
            out = out.split(sep)
            if type == KeyType.SET:
                out = set(out)
        if type != KeyType.MAP:
            return out
        d = {}
        for line in out:
            if not line:
                continue
            k, v = line.split(' ', 1)
            k = k.split('.')[-1]
            d[k] = v
        return d

class SimpleProgressBar(ProgressBar):
    """A simple progress bar that clears itself once finished."""

    def __init__(self, max_width=80, label='', *args, **kwargs):
        # Simple layout
        widgets = [label, Bar(left='[', right=']'), ' ', Percentage()]

        # Respect maximum width (if given)
        term_width = os.get_terminal_size().columns
        if max_width > 0 and term_width > max_width:
            term_width = max_width
        term_width = max_width

        super(SimpleProgressBar, self).__init__(
            widgets=widgets, term_width=term_width, *args, **kwargs)

    def finish(self, *args, **kwargs):
        # Clear the progress bar
        super(SimpleProgressBar, self).finish(
            end='\r{}\r'.format(self.term_width * ' '))

class ChangesetStore(dict):
    """A dictionary mapping commit hashes to changesets with local caching."""

    def __init__(self, path=None, text=False):
        if path is None:
            self.path = shell('git rev-parse --git-common-dir') + '/changeset'
        else:
            self.path = path
        self.text = text

        self.number_dir = '{}/numbers'.format(self.path)
        self.commit_dir = '{}/commits'.format(self.path)

        os.makedirs(self.number_dir, exist_ok=True)
        os.makedirs(self.commit_dir, exist_ok=True)

    def _data_file(self, number):
        if not number:
            return '/dev/null'
        return '{}/{}'.format(self.number_dir, number)

    def _link_file(self, commit):
        if len(commit) < SHA1_LEN:
            commit = shell('git rev-parse {}'.format(commit))
        base = '{}/{}'.format(self.commit_dir, commit[:2])
        os.makedirs(base, exist_ok=True)
        return '{}/{}'.format(base, commit[2:])

    def _fetch(self, commit):
        data = shell('gh pr list --state merged --limit 1 --json '
                     'number,title,url,labels --search {}'.format(commit))
        data = json.loads(data)
        if data:
            data = data[0]
        return data

    def load(self, commits, force=False, progress=False):
        """Fetch a list of commits and cache them."""
        for c in progressbar(commits, 'Fetching changesets ', not progress):
            if c in self and not force:
                continue
            self[c] = self._fetch(c)

    def __contains__(self, key):
        return os.path.islink(self._link_file(key))

    def __setitem__(self, key, data):
        number = 0 
        entry = ''

        if data:
            labels = sorted(x['name'] for x in data['labels'])
            if 'release' not in labels:
                number = data['number']
                entry = '{}\n{}\n{}'.format(
                    data['title'], ' '.join(labels), data['url'])

        if number:
            # Use a relative path to make the store portable
            target = '../../numbers/{}'.format(number)
        else:
            target = '/dev/null'

        data_file = self._data_file(number)
        link_file = self._link_file(key)

        # Ensure the write is atomic in case of SIGINT
        done = False
        while not done:
            try:
                with open(data_file, 'w') as f:
                    f.write(entry)
                if os.path.islink(link_file):
                    os.unlink(link_file)
                os.symlink(target, link_file)
                done = True
            except KeyboardInterrupt:
                continue

    def __getitem__(self, key):
        ret = {}
        link_file = self._link_file(key)

        if not os.path.islink(link_file):
            self.load([key])

        with open(link_file) as f:
            entry = list(map(str.strip, f.readlines()))

        data_file = os.readlink(link_file)
        if data_file == '/dev/null':
            raise KeyError(key)

        number = os.path.basename(data_file)

        if self.text:
            ret = '{}\n{}\n{}'.format(*entry)
        else:
            ret = {
                'number': number,
                'title': entry[0],
                'labels': set(entry[1].split()),
                'url': entry[2],
            }

        return ret


CONFIG = GitConfig('cherryPlan')
BACKPORT_RE = ['^\\(cherry picked from commit (.*)\\)$'] + \
              CONFIG.get('backportPatterns', KeyType.MULTILINE, [])
BACKPORT_RE = [re.compile(r, re.MULTILINE) for r in BACKPORT_RE]
SHA1_LEN = 40
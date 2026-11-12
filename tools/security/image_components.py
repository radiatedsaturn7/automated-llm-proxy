"""Read-only component evidence inside the final image; never a vulnerability waiver."""
import importlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess  # nosec B404 # Fixed allowlisted Perl import probes only; no shell or input payload.


PROGRAMS = ('perl', 'infocmp', 'mount', 'umount', 'nsenter', 'getfacl', 'setfacl', 'chacl')
MODULES = ('Pod::Text', 'Archive::Tar')
NATIVE_IMPORTS = ('ssl', 'sqlite3', 'asyncpg', 'cryptography', 'greenlet', 'uvloop', 'azure.identity.aio')
STATUS_FIELDS = {'Uid', 'Gid', 'CapEff', 'CapPrm', 'NoNewPrivs', 'Seccomp'}


def program_info(name):
    path = shutil.which(name)
    if path is None:
        return {'present': False}
    mode = Path(path).stat().st_mode
    return {'present': True, 'path': path, 'mode': oct(stat.S_IMODE(mode)),
            'setuid': bool(mode & stat.S_ISUID), 'setgid': bool(mode & stat.S_ISGID)}


def module_probe(module):
    if module not in MODULES:
        raise ValueError('unsupported module')
    perl = shutil.which('perl')
    if perl is None:
        return {'status': 'perl-unavailable'}
    # Fixed modules only, no untrusted shell, no document/archive processing.
    result = subprocess.run([perl, '-M' + module, '-e', 'exit 0'],  # nosec B603 # Module allowlist checked above; fixed argv, no shell, bounded timeout.
                            capture_output=True, text=True, timeout=10, check=False)
    expected = "Can't locate " + module.replace('::', '/') + '.pm in @INC'
    if result.returncode == 0:
        return {'status': 'loadable'}
    if expected in result.stderr:
        return {'status': 'not-found-in-default-inc'}
    raise RuntimeError('unexpected Perl module probe failure')


def proc_status(text):
    return {key: value.strip() for line in text.splitlines() if ':' in line
            for key, value in [line.split(':', 1)] if key in STATUS_FIELDS}


def collect():
    imports = {}
    for name in NATIVE_IMPORTS:
        importlib.import_module(name)  # Import failure is a real image smoke failure.
        imports[name] = True
    with sqlite3.connect(':memory:') as connection:
        connection.execute('create table smoke (id integer primary key, value text)')
        connection.execute('insert into smoke (value) values (?)', ('synthetic',))
        if connection.execute('select value from smoke').fetchone() != ('synthetic',):
            raise RuntimeError('SQLite smoke failure')
        connection.execute('update smoke set value = ? where id = 1', ('updated',))
        if connection.execute('select value from smoke').fetchone() != ('updated',):
            raise RuntimeError('SQLite update smoke failure')
        connection.execute('delete from smoke where id = 1')
        if connection.execute('select count(*) from smoke').fetchone() != (0,):
            raise RuntimeError('SQLite delete smoke failure')
        options = sorted(row[0] for row in connection.execute('pragma compile_options'))
    return {'schema_version': 1, 'purpose': 'component evidence, not CVE disposition',
            'uid': os.getuid(), 'gid': os.getgid(),
            'process': proc_status(Path('/proc/self/status').read_text()),
            'programs': {name: program_info(name) for name in PROGRAMS},
            'perl_modules': {name: module_probe(name) for name in MODULES},
            'sqlite': {'version': sqlite3.sqlite_version, 'compile_options': options,
                       'basic_crud_passed': True},
            'runtime_imports': imports,
            'limits': ['Default module lookup is not a complete filesystem inventory.',
                       'Runtime flags describe this CI invocation, not all deployments.',
                       'Component presence or absence alone does not settle a CVE.']}


if __name__ == '__main__':
    print(json.dumps(collect(), indent=2, sort_keys=True))

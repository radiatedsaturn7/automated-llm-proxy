"""Disposable volume preparation, run as root only inside fixture init container."""
import os
from pathlib import Path
import shutil

NAMES = ('DATABASE_URL', 'GATEWAY_MASTER_KEY', 'GATEWAY_SIGNING_KEY',
         'BOOTSTRAP_TOKEN', 'METRICS_TOKEN', 'OIDC_CLIENT_SECRET')


def project(source, target, uid=0, gid=10001):
    source, target = Path(source), Path(target)
    generation = target / '..installation-v1'
    # No in-place update/hot rotation: an old fixture volume must be destroyed.
    generation.mkdir(mode=0o700)
    for name in NAMES:
        output = generation / name
        shutil.copyfile(source / name, output)
        os.chown(output, uid, gid)
        output.chmod(0o440)
        (target / name).symlink_to('..data/' + name)
    (target / '..data').symlink_to(generation.name)
    for directory in (generation, target):
        os.chown(directory, uid, gid)
        directory.chmod(0o550)


if __name__ == '__main__':
    if os.geteuid() != 0:
        raise SystemExit('Disposable projection init requires container root')
    project('/source', '/installation')
    print('Disposable installation projection prepared')

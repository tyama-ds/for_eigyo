"""Run once with an installed Python; supports a proxy and offline wheels."""
import argparse
from pathlib import Path
import subprocess
import sys
import venv


def main():
    parser = argparse.ArgumentParser(description='Install Local LLM Chat dependencies')
    parser.add_argument('--proxy', help='HTTP(S) proxy for pip during setup')
    parser.add_argument('--certificate', help='Company CA PEM file for pip')
    parser.add_argument('--wheelhouse', help='Offline folder containing dependency wheels')
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error('Python 3.11 or newer is required.')
    root = Path(__file__).resolve().parent
    directory = root / '.venv'
    interpreter = directory / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    if not interpreter.exists():
        print('Creating local Python environment...', flush=True)
        venv.EnvBuilder(with_pip=True).create(directory)
    command = [str(interpreter), '-m', 'pip', 'install', '--only-binary=:all:', '-r', str(root / 'requirements-lock.txt')]
    if args.proxy:
        command.extend(['--proxy', args.proxy])
    if args.certificate:
        command.extend(['--cert', args.certificate])
    if args.wheelhouse:
        command.extend(['--no-index', '--find-links', args.wheelhouse])
    # Argument arrays keep shell metacharacters in paths out of command execution.
    return subprocess.call(command, cwd=root)


if __name__ == '__main__':
    raise SystemExit(main())

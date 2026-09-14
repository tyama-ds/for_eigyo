"""Start the installed app on this machine; used by the repository App Portal."""
import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent


def port_number(value):
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError('port must be between 1 and 65535')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description='Patent Atlas — local patent search workspace')
    parser.add_argument('--port', type=port_number, default=8810)
    parser.add_argument('--data-dir', type=Path, help='Persistent data directory (default: app data/)')
    args = parser.parse_args(argv)
    if args.data_dir:
        os.environ['PATENT_ATLAS_DATA'] = str(args.data_dir.expanduser().resolve())
    try:
        import uvicorn
    except ImportError:
        parser.exit(1, 'Install dependencies with this Python first: python -m pip install -r requirements.txt\n')
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    uvicorn.run('app:app', host='127.0.0.1', port=args.port, workers=1)


if __name__ == '__main__':
    main()

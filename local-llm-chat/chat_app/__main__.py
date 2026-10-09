import argparse
import json
import socket
import threading
import urllib.request
import webbrowser

import uvicorn


def main():
    parser = argparse.ArgumentParser(description='Local LLM Chat — localhost only')
    parser.add_argument('--port', type=int, default=8840)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    url = f'http://127.0.0.1:{args.port}'
    with socket.socket() as sock:
        try:
            sock.bind(('127.0.0.1', args.port))
        except OSError:
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(url + '/api/health', timeout=2) as response:
                    running = json.loads(response.read(4096)).get('app') == 'local-llm-chat'
            except (OSError, ValueError):
                running = False
            if running:
                print(f'Local LLM Chat is already running: {url}')
                if not args.no_browser:
                    webbrowser.open(url)
                return
            parser.exit(1, f'Port {args.port} is already in use. Close the previous app or use --port {args.port + 1}.\n')
    if not args.no_browser:
        timer = threading.Timer(1.5, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    print(f'Local LLM Chat: {url}\nPress Ctrl+C to stop.')
    uvicorn.run('chat_app.main:create_app', factory=True, host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()

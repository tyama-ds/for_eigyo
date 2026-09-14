"""Launch the local app and open a browser after the server is ready."""
import argparse
import threading
import time
import webbrowser

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Research Atlas")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    options = parser.parse_args()
    if not 1 <= options.port <= 65535:
        parser.error("Port must be 1–65535")
    server = uvicorn.Server(uvicorn.Config("app.main:app", host="127.0.0.1", port=options.port))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        while thread.is_alive() and not server.started:
            time.sleep(0.1)
        if server.started and not options.no_browser:
            webbrowser.open(f"http://127.0.0.1:{options.port}")
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        server.should_exit = True
        thread.join(10)


if __name__ == "__main__":
    main()

import os, http.server, socketserver
os.chdir(os.path.dirname(os.path.abspath(__file__)))
port = int(os.environ.get("PORT", 8765))
socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("", port), http.server.SimpleHTTPRequestHandler) as h:
    print(f"Serving on http://localhost:{port}")
    h.serve_forever()

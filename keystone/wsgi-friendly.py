#!/var/lib/openstack/bin/python3
"""Keystone WSGI application with friendly error pages for browser requests.

Wraps the standard Keystone WSGI application with middleware that intercepts
401 Unauthorized responses for browser requests (Accept: text/html) and returns
a user-friendly HTML page instead of the raw JSON API error.

API clients (Accept: application/json) are unaffected and continue to receive
standard Keystone error responses.
"""

from keystone.server.wsgi import initialize_public_application


_ERROR_401_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Access Denied</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #f5f5f5;
            color: #333;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            padding: 1rem;
        }
        .card {
            background: #fff;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            padding: 2.5rem;
            max-width: 480px;
            width: 100%;
        }
        h1 {
            font-size: 1.4rem;
            color: #c62828;
            margin-bottom: 1.25rem;
        }
        p { line-height: 1.6; margin-bottom: 0.75rem; }
        .ok { color: #2e7d32; font-weight: 500; }
        .help {
            background: #e3f2fd;
            border-left: 4px solid #1976d2;
            padding: 1rem 1.25rem;
            border-radius: 0 4px 4px 0;
            margin-top: 1.25rem;
        }
        .help p { margin-bottom: 0.25rem; }
        .help p:last-child { margin-bottom: 0; }
    </style>
</head>
<body>
    <div class="card">
        <h1>No Project Access</h1>
        <p class="ok">Your identity was verified successfully.</p>
        <p>
            You are not a member of any project in this environment
            and cannot proceed.
        </p>
        <div class="help">
            <p><strong>What to do:</strong></p>
            <p>
                Ask your project administrator to contact SUNET
                to request access on your behalf.
            </p>
        </div>
    </div>
</body>
</html>
"""


class _FriendlyErrorMiddleware:
    """Return user-friendly HTML for 401 errors on browser requests."""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        # Only intercept browser requests
        if 'text/html' not in environ.get('HTTP_ACCEPT', ''):
            return self.app(environ, start_response)

        captured = {}

        def capture_response(status, headers, exc_info=None):
            captured['status'] = status
            captured['headers'] = headers
            captured['exc_info'] = exc_info
            return lambda s: None

        result = self.app(environ, capture_response)

        if captured.get('status', '').startswith('401'):
            # Drain and close the original response
            try:
                for _ in result:
                    pass
            finally:
                if hasattr(result, 'close'):
                    result.close()

            body = _ERROR_401_HTML.encode('utf-8')
            start_response('401 Unauthorized', [
                ('Content-Type', 'text/html; charset=utf-8'),
                ('Content-Length', str(len(body))),
            ])
            return [body]

        # Non-401: pass through unchanged
        start_response(
            captured['status'],
            captured['headers'],
            captured.get('exc_info'),
        )
        return result


application = _FriendlyErrorMiddleware(initialize_public_application())

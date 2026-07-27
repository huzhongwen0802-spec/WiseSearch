# ExpertSearch OpenCLI Browser Bridge

ExpertSearch uses OpenCLI only as a limited fallback when normal HTTP homepage
access fails or the returned page does not contain the expert's name.

## One-Time Setup

1. Run `scripts\windows\start_opencli_browser.cmd`.
2. The dedicated Edge profile runs headlessly in the background while ExpertSearch is running.
3. Run `scripts\windows\check_opencli.cmd`. Both Extension and Connectivity must show `[OK]`.

This dedicated profile avoids exposing cookies and sessions from the user's
normal browser profile. It does not open a visible browser window.

## Runtime Behavior

- Normal `requests` access runs first.
- OpenCLI runs only after HTTP failure, unsupported content, empty text, or
  expert-name mismatch.
- Only public HTTP/HTTPS URLs are allowed. Localhost, private IPs, and local
  network addresses are blocked.
- Browser-rendered content is accepted only when the page contains the
  expert's name.
- If OpenCLI is unavailable, ExpertSearch continues with the existing Tavily
  and database workflow.

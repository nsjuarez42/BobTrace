# Instrument a new service

Inject `@bob_trace` into a directory of Python files and confirm tracing is active.

## Steps

1. Ask the user for the target directory if not already provided.
2. Run dry-run first to preview what will change:
   `wsl bash -c "cd /mnt/c/Users/Nicolas/Dev/trace_helper && source .venv/bin/activate && python main.py inject --target <dir> --dry-run"`
3. Show the summary line (functions and files that would be modified).
4. Confirm with the user before writing.
5. Run inject for real:
   `wsl bash -c "cd /mnt/c/Users/Nicolas/Dev/trace_helper && source .venv/bin/activate && python main.py inject --target <dir>"`
6. Report the summary (injected N functions across M files).
7. Tell the user to start their app and hit the failing endpoints, then run `/diagnose`.

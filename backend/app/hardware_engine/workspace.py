"""Use the existing Desktop Operator for VS Code, not a long-running shell."""
import asyncio
import json
from pathlib import Path


async def open_workspace(projects, project_id, desktop):
    root = projects.path(project_id)
    # The registry operator opens a real workspace file; code is still written
    # by filesystem, never keyboard paste. VS Code remains operator-owned.
    path = projects.write(project_id, 'kazumi.code-workspace', json.dumps({
        'folders': [{'path': '.'}], 'settings': {'task.allowAutomaticTasks': 'off'},
    }))
    result = await desktop.open_file(path, app='vscode')
    if not result.get('success'):
        return result
    from app.desktop.windows import list_visible_windows
    for _ in range(12):
        windows = await asyncio.to_thread(list_visible_windows)
        matched = [w for w in windows if 'visual studio code' in w.title.lower()
                   and (root.name.lower() in w.title.lower() or 'kazumi (workspace)' in w.title.lower())
                   and getattr(w, 'visible', True)]
        if matched:
            return {'success': True, 'effect_verified': True, 'source': 'desktop_window',
                    'workspace_file': path, 'window_title': matched[0].title}
        await asyncio.sleep(.5)
    return {'success': False, 'effect_verified': False, 'error_code': 'VSCODE_WINDOW_UNVERIFIED', 'workspace_file': path}

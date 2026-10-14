#!/usr/bin/env python3
"""Verify the installed server over MCP stdio, without operating the desktop."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import traceback
from pathlib import Path

from setup import write_private


async def verify(config, model=False):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # The local MCP server must inherit the user's login environment, not a
    # hand-copied credential. Never serialize this environment into the report.
    parameters = StdioServerParameters(command=sys.executable,
        args=['-m', 'echopath.mcp.server', '--config', str(config)], env=dict(os.environ), cwd=str(config.parent))
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            required = {'echopath_smoke_check', 'echopath_start_task', 'echopath_get_task', 'echopath_control_run'}
            if not required <= names:
                raise RuntimeError('The MCP server is missing required EchoPath tools.')
            result = await session.call_tool('echopath_smoke_check', {'include_model_bridge': model})
            if getattr(result, 'is_error', getattr(result, 'isError', False)):
                raise RuntimeError('The MCP smoke check returned an error.')
            data = getattr(result, 'structured_content', getattr(result, 'structuredContent', None))
            if not isinstance(data, dict) or 'import_ok' not in data:
                data = next((json.loads(item.text) for item in result.content if getattr(item, 'type', '') == 'text'), {})
            # Keep errors/CLI output out of the portable report: they can contain
            # credentials supplied by a third-party bridge.
            summary = {name: data.get(name) for name in ('import_ok', 'config_ok', 'model_bridge_checked', 'model_bridge_ok')}
            summary.update(mcp_ok=True, tools=len(names))
            summary['ok'] = bool(summary['import_ok'] and summary['config_ok'] and (not model or summary['model_bridge_ok']))
            if not summary['ok']:
                log = config.parent / 'local_data' / 'setup-verification-error.json'
                write_private(log, {key: data[key] for key in ('config_error', 'model_bridge_error') if key in data})
                summary['diagnostic_log'] = str(log)
            return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--model', action='store_true', help='Send one small request through the configured model bridge (uses quota).')
    args = parser.parse_args()
    try:
        result = asyncio.run(asyncio.wait_for(verify(args.config.resolve(), args.model), timeout=360 if args.model else 45))
    except Exception:
        log = args.config.resolve().parent / 'local_data' / 'setup-verification-error.json'
        write_private(log, {'error': traceback.format_exc()})
        result = {'ok': False, 'mcp_ok': False, 'diagnostic_log': str(log),
                  'message': 'MCP verification failed. Check the private diagnostic and rerun after fixing the cause.'}
    print(json.dumps(result, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

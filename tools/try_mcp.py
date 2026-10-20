"""Developer helper: Grainy with the AI connection (MCP) on a temporary library, for end-to-end runs.

    .venv\\Scripts\\python.exe tools\\try_mcp.py                       # prints the install commands for this Grainy
    .venv\\Scripts\\python.exe tools\\try_mcp.py --run codex           # also starts that client in this terminal
    .venv\\Scripts\\python.exe tools\\try_mcp.py --run claude --http   # over HTTP with the key instead of the bridge

The temporary Grainy keeps its settings and its bridge in the temporary folder, so the install commands it prints
name that folder (a normal install's commands are in docs/mcp-install.json). Nothing is registered with the clients
and nothing touches the real library or settings: Claude Code reads a temporary --mcp-config file, Codex gets -c
overrides. With --http the key goes into that file (Claude Code) or an environment variable of the Codex process
only; it is never printed. See DEVELOPMENT.md for the manual checklist.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def photos(folder,count=6):
    import numpy as np
    from PIL import Image
    folder.mkdir(parents=True,exist_ok=True)
    yy,xx=np.mgrid[:400,:600]
    for n in range(count):
        tint=np.array([1,.85+.05*n,.7+.05*n])
        base=.15+.7*(xx/600*.7+yy/400*.3)
        Image.fromarray(np.uint8(np.clip(base[...,None]*tint,0,1)*255)).save(folder/f'roll-{n+1:02d}.png')
    return folder


def main():
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--run',choices=['claude','codex'],help='start this client as a child process')
    parser.add_argument('--http',action='store_true',help='connect over HTTP with the key instead of the bridge')
    args=parser.parse_args()
    from PySide6.QtWidgets import QApplication
    from luma.app import configure_application
    from luma.preferences import Preferences
    from luma.desktop_session import DesktopSession
    from luma import mcp_service
    from luma.mcp_dialog import install_commands,install_text
    root=Path(tempfile.mkdtemp(prefix='grainy-mcp-'))
    mcp_service.pictures_folder=lambda:root/'Pictures'/'Grainy'      # AI exports stay in the temporary folder
    mcp_service.DEFAULT_PORT=0                                        # a free port: 47821 stays the real Grainy's
    app=QApplication(sys.argv);app.setApplicationName('Grainy');configure_application(app)
    preferences=Preferences(root/'settings.json')
    preferences.save_language('ko');preferences.save('update_check',False)
    session=DesktopSession(app,root/'library',preferences)
    if not session.start():return 1                                  # the connection is on by default
    if not session.mcp.running:
        print('AI connection did not start:',session.mcp.error);return 1
    session.window.import_paths([str(photos(root/'photos'))])
    windows=sys.platform=='win32';commands=install_commands(windows,session.mcp.folder)
    print(f'Temporary library: {root}')
    print(f'AI connection: port {session.mcp.bound_port}, bridge {session.mcp.bridge_path()}')
    print('Install commands for this temporary Grainy (they would register it; not run here):')
    print('  Claude Code:',commands['claude_code'])
    print('  Codex:',commands['codex'])
    print('  Claude Desktop:',install_text(commands['claude_desktop_json']).replace('\n','\n  '))
    env=dict(os.environ)
    if args.http:
        url=f'http://127.0.0.1:{session.mcp.bound_port}/mcp';token=session.mcp.token
        server={'type':'http','url':url,'headers':{'Authorization':f'Bearer {token}'}}
        codex=['codex','-c',f'mcp_servers.grainy.url="{url}"','-c','mcp_servers.grainy.bearer_token_env_var="GRAINY_MCP_TOKEN"']
        env['GRAINY_MCP_TOKEN']=token
    else:
        stdio=commands['claude_desktop_json']['mcpServers']['grainy']
        server={'type':'stdio','command':stdio['command'],'args':stdio['args']}
        codex=['codex','-c',f'mcp_servers.grainy.command={json.dumps(stdio["command"])}',
               '-c',f'mcp_servers.grainy.args={json.dumps(stdio["args"],ensure_ascii=False)}']
    config=root/'claude-mcp.json'
    config.write_text(json.dumps({'mcpServers':{'grainy':server}}),encoding='utf-8')
    claude=['claude','--strict-mcp-config','--mcp-config',str(config)]
    print('Without registering anything:')
    print('  Claude Code:',subprocess.list2cmdline(claude))
    print('  Codex:',subprocess.list2cmdline(codex)+('  (GRAINY_MCP_TOKEN for that process only; --run codex sets it)' if args.http else ''))
    child=None
    if args.run:
        command=claude if args.run=='claude' else codex
        if args.run=='claude':env.pop('GRAINY_MCP_TOKEN',None)
        child=subprocess.Popen(command,env=env,shell=sys.platform=='win32')
        from PySide6.QtCore import QTimer
        timer=QTimer();timer.setInterval(500);timer.timeout.connect(lambda:child.poll() is not None and session.window.close())
        timer.start()
    code=app.exec()
    if child is not None and child.poll() is None:child.terminate()
    return code


if __name__=='__main__':raise SystemExit(main())

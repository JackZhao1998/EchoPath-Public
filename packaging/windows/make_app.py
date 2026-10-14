"""Create a per-user Start menu shortcut to the Windows tray app."""
import argparse
import base64
import os
import subprocess
from pathlib import Path


def make_shortcut(root, config, destination):
    python = root / '.venv/Scripts/pythonw.exe'
    if not python.is_file() or not config.is_file():
        raise ValueError('Install EchoPath and create its local configuration first.')
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / 'EchoPath.lnk'
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    script = ("$ErrorActionPreference='Stop'; $shell=New-Object -ComObject WScript.Shell; "
              '$shortcut=$shell.CreateShortcut(' + quote(target) + '); '
              '$shortcut.TargetPath=' + quote(python) + '; '
              '$shortcut.Arguments=' + quote('-m echopath.app --config "' + str(config) + '"') + '; '
              '$shortcut.WorkingDirectory=' + quote(root) + '; '
              "$shortcut.Description='EchoPath task controls'; $shortcut.Save()")
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                    base64.b64encode(script.encode('utf-16-le')).decode('ascii')],
                   check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--dest', default=str(Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs'))
    args = parser.parse_args()
    print(make_shortcut(Path(__file__).resolve().parents[2], Path(args.config).resolve(), Path(args.dest).expanduser()))

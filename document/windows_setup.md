# Windows app and VM setup

The Windows tray app and managed VirtualBox guest installer are provided by the
[`windows-version` branch](https://github.com/JackZhao1998/EchoPath-Public/tree/windows-version).
This `main` branch provides the macOS app; its installer accepts `--app` only
on macOS and does not include the Windows tray/VirtualBox modules.

For Windows, follow the [Windows installation guide](https://github.com/JackZhao1998/EchoPath-Public/blob/windows-version/INSTALL.md)
and [Windows app and VM guide](https://github.com/JackZhao1998/EchoPath-Public/blob/windows-version/document/windows_setup.md).
Clone that branch into a new permanent folder rather than mixing files from
the two releases:

```powershell
git clone --branch windows-version --single-branch https://github.com/JackZhao1998/EchoPath-Public.git EchoPath
Set-Location EchoPath
python packaging/setup.py install --backend codex --agent codex --app
```

The core CLI and web Control Room can run on Windows from this branch without
`--app`. For the complete Windows app experience, use the Windows branch above.

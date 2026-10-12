from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_dynamic_libs

root = Path(SPECPATH)
a = Analysis(
    [str(root / 'scripts/backend_entry.py')],
    pathex=[str(root / 'src')],
    binaries=collect_dynamic_libs('sherpa_onnx'),
    datas=collect_data_files('pypdfium2_raw') + collect_data_files('mcp') + collect_data_files('faster_whisper') + collect_data_files('mynotes', includes=['skills/**']),
    hiddenimports=collect_submodules('mynotes') + collect_submodules('uvicorn'),
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='mynotes',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='mynotes')

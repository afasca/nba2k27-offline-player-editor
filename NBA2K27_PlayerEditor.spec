# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['nba2k27_player_editor.py'],
    pathex=[],
    binaries=[],
    datas=[('player_fields.json', '.'), ('player_extra_fields.json', '.'),
           ('player_advanced_fields.json', '.'), ('player_appearance_fields.json', '.'),
           ('player_signature_options.json', '.'), ('playbook_plays.json', '.'), ('staff_fields.json', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='NBA2K27_PlayerEditor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

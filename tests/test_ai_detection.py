import os
import pytest


@pytest.mark.parametrize('folder',['bin','codex'])                     # Codex 0.163 and later, and before
@pytest.mark.skipif(os.name!='nt',reason='the Windows search')
def test_windows_finds_codex_exe_in_the_npm_package(tmp_path,monkeypatch,folder):
    from luma import auth
    package=tmp_path/'npm'/'node_modules'/'@openai'/'codex'
    native=package/'node_modules'/'@openai'/'codex-win32-x64'/'vendor'/'x86_64-pc-windows-msvc'/folder/'codex.exe'
    native.parent.mkdir(parents=True);native.write_bytes(b'MZ native')
    (package/'node_modules'/'@openai'/'codex-win32-x64'/'vendor'/'x86_64-pc-windows-msvc'/'codex-resources').mkdir()
    (package/'node_modules'/'@openai'/'codex-win32-x64'/'vendor'/'x86_64-pc-windows-msvc'/'codex-resources'/'codex-command-runner.exe').write_bytes(b'MZ')
    monkeypatch.setenv('APPDATA',str(tmp_path));monkeypatch.setattr(auth.shutil,'which',lambda name:None)
    assert auth.find_codex()==str(native.resolve())


@pytest.mark.skipif(os.name=='nt',reason='the macOS search; Windows looks for codex.exe')
def test_macos_finds_the_native_program_behind_the_npm_command(tmp_path,monkeypatch):
    from luma import auth
    package=tmp_path/'lib'/'node_modules'/'@openai'/'codex'
    native=package/'node_modules'/'@openai'/'codex-darwin-arm64'/'vendor'/'aarch64-apple-darwin'/'bin'/'codex'   # Codex 0.163
    native.parent.mkdir(parents=True)
    native.write_bytes(b'\xcf\xfa\xed\xfe native');native.chmod(0o755)
    script=package/'bin'/'codex.js';script.parent.mkdir();script.write_text('#!/usr/bin/env node\n');script.chmod(0o755)
    command=tmp_path/'bin'/'codex';command.parent.mkdir();command.symlink_to(script)
    monkeypatch.setattr(auth.shutil,'which',lambda name:str(command) if name=='codex' else None)
    assert auth.find_codex()==str(native.resolve())                    # not the Node script the command points to
    native.unlink()
    monkeypatch.setattr(auth.Path,'home',lambda:tmp_path/'nobody')
    found=auth.find_codex()
    assert found is None or 'node_modules' not in found and found!=str(script)   # only a program installed on this Mac itself
    assert auth.find_codex(str(script)) is None and auth.find_codex(str(tmp_path/'missing')) is None
    program=tmp_path/'codex';program.write_bytes(b'\xcf\xfa\xed\xfe');program.chmod(0o755)
    assert auth.find_codex(str(program))==str(program.resolve())
    monkeypatch.setattr(auth.shutil,'which',lambda name:str(program) if name=='codex' else None)
    assert auth.find_codex()==str(program.resolve())                   # a native program installed as `codex`

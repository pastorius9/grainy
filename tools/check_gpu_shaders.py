"""Compile the HLSL embedded in native/gpu_compute.cpp and print compiler errors (developer aid)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gpu_shaders import compile_shader, sources

failed = False
for name, text in sources().items():
    code, messages = compile_shader(name, text)
    print(f'{name}: {"ok" if code else "FAILED"}')
    if messages.strip():
        print(messages)
    failed |= code is None
sys.exit(1 if failed else 0)

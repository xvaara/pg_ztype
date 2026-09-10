#!/usr/bin/env python3
"""Print the AddressSanitizer runtime library the given compiler ships, or nothing.

The runtime must be preloaded into an ordinary PostgreSQL server before a sanitized module
is dlopen'ed; the Makefile's test-asan target passes the path through ZTYPE_SANITIZER_RUNTIME.
"""
import subprocess
import sys

NAMES = {'darwin': ['libclang_rt.asan_osx_dynamic.dylib'],
         'linux': ['libclang_rt.asan-x86_64.so', 'libclang_rt.asan-aarch64.so', 'libclang_rt.asan.so', 'libasan.so']}


def runtime(cc):
    for name in NAMES.get(sys.platform, NAMES['linux']):
        try:
            path = subprocess.run([cc, f'-print-file-name={name}'], capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return ''
        if path and path != name and '/' in path:
            try:
                with open(path, 'rb'):
                    return path
            except OSError:
                continue
    return ''


if __name__ == '__main__':
    print(runtime(sys.argv[1] if len(sys.argv) > 1 else 'clang'))

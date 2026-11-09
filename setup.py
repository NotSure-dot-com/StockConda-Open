from setuptools import setup, Extension, find_packages
from Cython.Build import cythonize
import numpy as np
import sys

# Platform-specific compile args
if sys.platform == 'win32':
    # MSVC (Windows) flags
    extra_compile_args = ['/O2', '/wd4551']  # Equivalent to -O3, suppress C4551 warning
else:
    # GCC/Clang (Linux/macOS) flags
    extra_compile_args = ['-O3', '-march=native']

# Cython compiler directives
compiler_directives = {
    'language_level': '3',
    'boundscheck': False,
    'wraparound': False,
    'cdivision': True,
    'initializedcheck': False,
    'nonecheck': False
}

# Define extensions
extensions = [
    Extension(
        "chess_engine.cython_ext.evaluation",
        ["src/chess_engine/cython_ext/evaluation.pyx", "src/chess_engine/cython_ext/cython_chess_backend.cpp"],
        include_dirs=[np.get_include(), "src/chess_engine/cython_ext"],
        extra_compile_args=extra_compile_args,
        define_macros=[("NPY_NO_DEPRECATED_API", "NPY_1_7_API_VERSION")],
        language="c++"
    ),

    Extension(
        "chess_engine.cython_ext.transposition",
        ["src/chess_engine/cython_ext/transposition.pyx"],
        extra_compile_args=extra_compile_args,
        define_macros=[("NPY_NO_DEPRECATED_API", "NPY_1_7_API_VERSION")],
    ),
    # Temporarily exclude search_state extension to unblock builds during regression triage
    # Extension(
    #     "chess_engine.cython_ext.search_state",
    #     ["src/chess_engine/cython_ext/search_state.pyx", "src/chess_engine/cython_ext/cython_chess_backend.cpp"],
    #     include_dirs=["src/chess_engine/cython_ext"],
    #     extra_compile_args=extra_compile_args,
    #     define_macros=[("NPY_NO_DEPRECATED_API", "NPY_1_7_API_VERSION")],
    #     language="c++"
    # ),
    Extension(
        "chess_engine.cython_ext.cython_chess",
        ["src/chess_engine/cython_ext/cython_chess.pyx", "src/chess_engine/cython_ext/cython_chess_backend.cpp"],
        include_dirs=[np.get_include()],
        extra_compile_args=extra_compile_args + ['/std:c++14'] if sys.platform == 'win32' else [],
        define_macros=[("NPY_NO_DEPRECATED_API", "NPY_1_7_API_VERSION")],
        language="c++"
    )
]

# Setup configuration
setup(
    name="chess_engine",
    version="0.2.3.5.dev1",
    ext_modules=cythonize(
        extensions,
        compiler_directives=compiler_directives,
        annotate=True  # Generates HTML annotation
    ),
    zip_safe=False,  # Required for Cython
)

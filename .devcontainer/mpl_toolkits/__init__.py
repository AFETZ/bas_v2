"""Prefer the hash-locked user Matplotlib while retaining namespace members."""

from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)

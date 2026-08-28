"""The version, in one place.

Kept out of ``__init__`` so that ``http`` can read it without importing the
package (which imports ``http`` right back), and out of installed metadata so
that a source checkout does not announce whatever version happens to be
pip-installed alongside it.
"""

__version__ = "1.0.0"

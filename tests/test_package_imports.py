"""Every module in the package must import with only the declared dependencies.

Modules that have no tests of their own are never loaded by the rest of the
suite, so an import of a third-party package that pyproject.toml does not
declare passes CI and then fails for anyone who installs the package. Each
module is imported here once.
"""

import importlib
import pkgutil

import pytest

import osint_agent

MODULE_NAMES = sorted(
    module.name
    for module in pkgutil.walk_packages(osint_agent.__path__, "osint_agent.")
)


def test_module_discovery_finds_modules_without_their_own_tests():
    # Guards against a vacuous pass: an empty list would skip the test below.
    expected = {
        "osint_agent.models",
        "osint_agent.docs_ingest.extract",
        "osint_agent.tools.ice_leadership",
    }

    assert expected <= set(MODULE_NAMES)


@pytest.mark.parametrize("module_name", MODULE_NAMES)
def test_module_imports_with_declared_dependencies(module_name):
    importlib.import_module(module_name)

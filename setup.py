import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from setuptools import setup
from setuptools.command.build_py import build_py

from tools.build_provenance import write_to


class BuildPyWithProvenance(build_py):
    def run(self):
        super().run()
        if not self.dry_run:
            write_to(os.path.join(self.build_lib, "pangpang"))


setup(cmdclass={"build_py": BuildPyWithProvenance})

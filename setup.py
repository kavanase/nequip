from setuptools import setup, find_packages
from pathlib import Path

# see https://packaging.python.org/guides/single-sourcing-package-version/
version_dict = {}
with open(Path(__file__).parents[0] / "nequip/_version.py") as fp:
    exec(fp.read(), version_dict)
version = version_dict["__version__"]
del version_dict

setup(
    name="nequip",
    version=version,
    description="NequIP is an open-source code for building E(3)-equivariant interatomic potentials.",
    download_url="https://github.com/mir-group/nequip",
    author="Simon Batzner, Albert Musealian, Lixin Sun, Anders Johansson, Mario Geiger, Tess Smidt",
    python_requires=">=3.7",
    packages=find_packages(include=["nequip", "nequip.*"]),
    entry_points={
        # make the scripts available as command line scripts
        "console_scripts": [
            "nequip-train = nequip.scripts.train:main",
            "nequip-benchmark = nequip.scripts.benchmark:main",
            "nequip-deploy = nequip.scripts.deploy:main",
        ]
    },
    install_requires=[
        "numpy",
        "matscipy",
        "ase",
        "tqdm",
        "e3nn>=0.4.4,<0.6.0",
        "pyyaml",
        "contextlib2;python_version<'3.7'",  # backport of nullcontext
        'contextvars;python_version<"3.7"',  # backport of contextvars for savenload
        "typing_extensions;python_version<'3.8'",  # backport of Final
        "importlib_metadata;python_version<'3.10'",  # backport of importlib
        "hydra-core",
        "lightning",
        "torchmetrics",
    ],
    zip_safe=True,
)

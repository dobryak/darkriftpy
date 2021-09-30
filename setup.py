from setuptools import setup, find_packages

__VERSION__ = "0.1.0"
__LICENSE__ = "GPL3"
__AUTHOR__ = __MAINTAINER__ = "Anton Dobriakov"
__AUTHOR_EMAIL__ = "anton.dobryakov@gmail.com"
__URL__ = "https://github.com/dobryak/darkriftpy"


def get_install_requires():
    install_requires = []

    with open("requirements.txt", "r") as fp:
        for pkg in fp:
            install_requires.append(pkg)

    return install_requires


setup(
    name="darkriftpy",
    description="DarkriftPy is a Python implementation of DarkRift2",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url=__URL__,
    author=__AUTHOR__,
    author_email=__AUTHOR_EMAIL__,
    version=__VERSION__,
    license=__LICENSE__,
    packages=find_packages(exclude=["tests*"]),
    package_data={"darkriftpy": ["py.typed"]},
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Framework :: AsyncIO",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
    ],
    install_requires=get_install_requires(),
    python_requires=">=3.9",
)

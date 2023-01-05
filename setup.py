import setuptools

setuptools.setup(
    name="portwrap",
    version="0.0.1",
    url="https://github.com/ryanlovett/portwrap",
    author="Ryan Lovett",
    author_email="rylo@berkeley.edu",
    description="Sandbox ports behind a proxy",
    packages=setuptools.find_packages(),
    keywords=["bwrap", "slirp4netns", "nsenter"],
    entry_points={
        "console_scripts": [
            "portwrap = portwrap.__main__:main",
        ]
    },
)

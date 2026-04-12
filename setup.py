# setup.py
from setuptools import setup, find_packages

setup(
    name="pr-reviewer",
    version="0.1.0",
    packages=find_packages(exclude=["tests*"]),
    install_requires=[
        "httpx>=0.27",
        "anthropic>=0.25",
        "click>=8.1",
        "rich>=13",
        "pydantic-settings>=2.0",
    ],
    entry_points={
        "console_scripts": [
            "pr-review=pr_reviewer.cli:cli",
        ],
    },
    python_requires=">=3.9",
)
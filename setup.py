#!/usr/bin/env python

from setuptools import setup

import EasyTweeter

with open("README.md", "r") as fh:
    long_description = fh.read()

setup(name='EasyTweeter',
      version=EasyTweeter.__version__,
      description="EasyTweeter is a simple, one-file library which automates almost all of Twitter bot construction for simple bots which just post tweets, and don't do much (or any) interaction with followers, other than notifying the bot's owner that it is being interacted with.",
	  long_description=long_description,
	  long_description_content_type="text/markdown",
	  license='MIT',
      author='Daniel Westbrook',
      author_email='dan@pixelatedawesome.com',
      url='https://github.com/HelloLobsterDog/EasyTweeter',
      py_modules=['EasyTweeter'],
      classifiers=[
          "Development Status :: 4 - Beta",
		  "Programming Language :: Python :: 3",
          "License :: OSI Approved :: MIT License",
          "Operating System :: OS Independent"
      ],
	  install_requires=[
		  "tweepy>=3.8.0"
	  ]
)

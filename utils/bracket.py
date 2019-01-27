#!/usr/bin/env python

import os
import sys

# Hack to make this Python program executable from the utils subdirectory
basepath, _ = os.path.split(os.path.realpath(__file__))
_UTILS = os.sep + "utils"
if basepath.endswith(_UTILS):
    basepath = basepath[0:-len(_UTILS)]
    sys.path.append(basepath)

from settings import Settings
from scraperdb import SessionContext
from treeutil import TreeUtility as tu

Settings.read("config/Reynir.conf")
Settings.DEBUG = False

TEXT = "Ég bý í Baugatanga 6. Hér er prófun á þáttun texta."

with SessionContext(read_only = True) as session:
    pgs, stats = tu.parse_text_to_bracket_form(session, TEXT)

for pg in pgs:
    for sent in pg:
        print(sent)


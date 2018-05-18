#!/usr/bin/env python

from __future__ import print_function
from glob import glob
from tempfile import NamedTemporaryFile
import os
import sys

from alibuild_helpers.log import debug, error, info
from alibuild_helpers.utilities import format
from alibuild_helpers.cmd import execute
from alibuild_helpers.utilities import (
    detectArch, parseRecipe, getRecipeReader
)


def deps(recipesDir, topPackage, outFile, buildRequires, transRed, disable):
    dot = {}
    keys = ["requires"]
    if buildRequires:
        keys.append("build_requires")

    for p in glob("%s/*.sh" % recipesDir):
        debug(format("Reading file %(filename)s", filename=p))
        try:
            err, recipe, _ = parseRecipe(getRecipeReader(p))
            name = recipe["package"]
            if name in disable:
                debug("Ignoring %s, disabled explicitly" % name)
                continue
        except Exception as e:
            msg = "Error reading recipe: {0}: {1}: {2}"
            msg = msg.format(p, type=type(e).__name__, msg=str(e))
            error(msg)
            sys.exit(1)

        dot[name] = dot.get(name, [])
        for k in keys:
            for d in recipe.get(k, []):
                d = d.split(":")[0]
                d in disable or dot[name].append(d)

    selected = None
    if topPackage != "all":
        if topPackage not in dot:
            error("Package {0} does not exist".format(topPackage))
            return False

        selected = [topPackage]
        olen = 0
        while len(selected) != olen:
            olen = len(selected)
            selected += [x for s in selected if s in dot
                         for x in dot[s] if x not in selected]
        selected.sort()

    result = "digraph {\n"
    for p, deps in list(dot.items()):
        if selected and p not in selected:
            continue

        result += "  \"%s\";\n" % p
        for d in deps:
            result += "  \"%s\" -> \"%s\";\n" % (p, d)
    result += "}\n"

    with NamedTemporaryFile(delete=False) as fp:
        fp.write(result)

    dotFileName = fp.name
    try:
        if transRed:
            cmd = "tred {0} > {0}.0 && mv {0}.0 {0}".format(dotFileName)
            execute(cmd)
        execute(["dot", dotFileName, "-Tpdf", "-o", outFile])
        info("Dependencies graph generated: {0}".format(outFile))

    except Exception as e:
        msg = "Error generating dependencies with dot: {0}: {1}"
        error(msg.format(type(e).__name__, str(e)))

    os.remove(dotFileName)
    return True


def depsArgsParser(parser):
    parser.add_argument("topPackage")
    parser.add_argument("-a", "--architecture",
                        help="force architecture",
                        dest="architecture",
                        default=detectArch())

    parser.add_argument("--dist",
                        dest="distDir",
                        default="alidist",
                        help="Recipes directory")

    parser.add_argument("--output-file", "-o",
                        dest="outFile",
                        default="dist.pdf",
                        help="Output file (PDF format)")

    parser.add_argument("--debug", "-d",
                        dest="debug",
                        action="store_true",
                        default=False,
                        help="Debug output")

    parser.add_argument("--build-requires", "-b",
                        dest="buildRequires",
                        action="store_true",
                        default=False,
                        help="Debug output")

    parser.add_argument("--neat",
                        dest="neat",
                        action="store_true",
                        default=False,
                        help="Neat graph with transitive reduction")

    parser.add_argument("--disable",
                        dest="disable",
                        default=[],
                        help="List of packages to ignore")

    parser.add_argument("--chdir", "-C",
                        help="Change to the specified directory first",
                        dest="chdir",
                        metavar="DIR",
                        default=os.environ.get("ALIBUILD_CHDIR", "."))
    return parser

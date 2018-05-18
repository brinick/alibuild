from os import makedirs, unlink, readlink, rmdir
from os.path import abspath, exists, basename, dirname, join, realpath
try:
    from commands import getstatusoutput
    from urllib2 import urlopen, URLError
except ImportError:
    from subprocess import getstatusoutput
    from urllib.request import urlopen
    from urllib.error import URLError

from datetime import datetime
from glob import glob
try:
    from collections import OrderedDict
except ImportError:
    from ordereddict import OrderedDict

import socket
import os
import ssl
import json
import re
import shutil
import sys
import time
import yaml

from alibuild_helpers.analytics import report_event
from alibuild_helpers.log import (
    debug, error, info, banner, warning, dieOnError,
    logger_handler, LogFormatter, ProgressPrint, riemannStream
)

from alibuild_helpers.cmd import execute, getStatusOutputBash, BASH
from alibuild_helpers.utilities import (
    prunePaths, format, dockerStatusOutput, parseDefaults, readDefaults,
    getPackageList, validateDefaults, Hasher, yamlDump
)

from alibuild_helpers.workarea import updateReferenceRepoSpec


def star():
    return basename(sys.argv[0]).lower().replace("build", "")


def gzip():
    return getstatusoutput("which pigz")[0] and "gzip" or "pigz"


def tar():
    tar_cmd = "tar --ignore-failed-read -cvvf /dev/null /dev/zero"
    return getstatusoutput(tar_cmd)[0] and "tar" or "tar --ignore-failed-read"


def writeAll(fn, txt):
    f = open(fn, "w")
    f.write(txt)
    f.close()


def readHashFile(fn):
    try:
        return open(fn).read().strip("\n")
    except IOError:
        return "0"


def getDirectoryHash(d):
    if exists(join(d, ".git")):
        err, out = getstatusoutput("GIT_DIR=%s/.git git rev-parse HEAD" % d)
        dieOnError(err, "Impossible to find reference for %s " % d)
    else:
        cmd = (
            "pip --disable-pip-version-check show alibuild | "
            "grep -e \"^Version:\" | "
            "sed -e 's/.* //'"
        )

        err, out = getstatusoutput(cmd)
        dieOnError(err, "Impossible to find reference for %s " % d)
    return out


class NoRemoteSync:
    """Helper class which does not do anything to sync"""
    def syncToLocal(self, p, spec):
        pass

    def syncToRemote(self, p, spec):
        pass


class HttpRemoteSync:
    def __init__(self, remoteStore, architecture, workdir, insecure):
        self.remoteStore = remoteStore
        self.writeStore = ""
        self.architecture = architecture
        self.workdir = workdir
        self.insecure = insecure

    def syncToRemote(self, p, spec):
        return

    def syncToLocal(self, p, spec):
        debug("Updating remote store for package %s@%s" % (p, spec["hash"]))
        hashListUrl = "{rs}/{sp}/".format(rs=self.remoteStore,
                                          sp=spec["storePath"])
        pkgListUrl = "{rs}/{sp}/".format(rs=self.remoteStore,
                                         sp=spec["linksPath"])
        hashList = []
        pkgList = []

        try:
            if self.insecure:
                context = ssl._create_unverified_context()
                hashList = json.loads(
                    urlopen(hashListUrl, context=context).read()
                )
                pkgList = json.loads(
                    urlopen(pkgListUrl, context=context).read()
                )
            else:
                hashList = json.loads(urlopen(hashListUrl).read())
                pkgList = json.loads(urlopen(pkgListUrl).read())
        except URLError as e:
            debug(
                "Cannot find precompiled package for %s@%s" % (p, spec["hash"])
            )
        except Exception as e:
            info(e)
            error("Unknown response from server")

        cmd = "mkdir -p {hd} && mkdir -p {ld}".format(
            hd=spec["tarballHashDir"],
            ld=spec["tarballLinkDir"]
        )

        execute(cmd)
        hashList = [x["name"] for x in hashList]

        for pkg in hashList:
            cmd = (
                "curl {i} -o {hd}/{n} -L {rs}/{sp}/{n}\n"
            ).format(
                i="-k" if self.insecure else "",
                n=pkg,
                sp=spec["storePath"],
                rs=self.remoteStore,
                hd=spec["tarballHashDir"]
            )
            debug(cmd)
            execute(cmd)

        for pkg in pkgList:
            if pkg["name"] in hashList:
                cmd = (
                    "ln -sf ../../{a}/store/{sh}/{h}/{n} {ld}/{n}\n"
                ).format(
                    a=self.architecture,
                    h=spec["hash"],
                    sh=spec["hash"][0:2],
                    n=pkg["name"],
                    ld=spec["tarballLinkDir"]
                )
            else:
                cmd = (
                    "ln -s unknown {ld}/{n} 2>/dev/null || true\n"
                ).format(ld=spec["tarballLinkDir"], n=pkg["name"])

            execute(cmd)


class RsyncRemoteSync:
    """Helper class to sync package build directory using RSync."""
    def __init__(self, remoteStore, writeStore,
                 architecture, workdir, rsyncOptions):
        self.remoteStore = re.sub("^ssh://", "", remoteStore)
        self.writeStore = re.sub("^ssh://", "", writeStore)
        self.architecture = architecture
        self.rsyncOptions = rsyncOptions
        self.workdir = workdir

    def syncToLocal(self, p, spec):
        debug("Updating remote store for package %s@%s" % (p, spec["hash"]))
        cmds = [
            "mkdir -p {hashDir}",
            "rsync -av {ro} {remoteStore}/{storePath}/ {hashDir}/ || true"
            "rsync -av --delete {ro} {remoteStore}/{links}/ {linkDir}/ || true"
        ]
        cmd = "\n".join(cmds).format(
            ro=self.rsyncOptions,
            remoteStore=self.remoteStore,
            storePath=spec["storePath"],
            links=spec["linksPath"],
            hashDir=spec["tarballHashDir"],
            linkDir=spec["tarballLinkDir"]
        )

        err = execute(cmd)
        dieOnError(err, "Unable to update from specified store.")

    def syncToRemote(self, p, spec):
        if not self.writeStore:
            return

        tarballNameWithRev = (
            "{package}-{version}-{revision}.{architecture}.tar.gz"
        ).format(architecture=self.architecture, **spec)

        cmd = (
            "cd {workdir} && "
            "rsync -avR {o} --ignore-existing {store}/{withRev} {rStore}/ &&"
            "rsync -avR {o} --ignore-existing {links}/{withRev} {rStore}/"
        ).format(
            workdir=self.workdir,
            rStore=self.remoteStore,
            o=self.rsyncOptions,
            store=spec["storePath"],
            links=spec["linksPath"],
            withRev=tarballNameWithRev
        )

        err = execute(cmd)
        dieOnError(err, "Unable to upload tarball.")


def createDistLinks(spec, specs, args, repoType, requiresType):
    """Creates a directory in the store which contains symlinks to the package
    and its direct / indirect dependencies
    """
    target = "TARS/{arch}/{repo}/{pkg}/{pkg}-{vers}-{rev}".format(
        arch=args.architecture,
        repo=repoType,
        pkg=spec["package"],
        vers=spec["version"],
        rev=spec["revision"]
    )

    shutil.rmtree(target, True)
    for x in [spec["package"]] + list(spec[requiresType]):
        dep = specs[x]
        source = (
            "../../../../../TARS/{a}/store/{sh}/{h}/{p}-{v}-{r}.{a}.tar.gz",
        ).format(
            a=args.architecture,
            sh=dep["hash"][0:2],
            h=dep["hash"],
            p=dep["package"],
            v=dep["version"],
            r=dep["revision"]
        )

        cmd = (
            "cd {workDir} &&"
            "mkdir -p {target} &&"
            "ln -sfn {source} {target}"
        ).format(workDir=args.workDir, target=target, source=source)
        execute(cmd)

    rsyncOptions = ""
    if args.writeStore:
        cmd = "cd {w} && rsync -avR {o} --ignore-existing {t}/ {rs}/".format(
            w=args.workDir,
            rs=args.writeStore,
            o=rsyncOptions,
            t=target
        )
        execute(cmd)


def doBuild(args, parser):
    if args.remoteStore.startswith("http"):
        syncHelper = HttpRemoteSync(args.remoteStore,
                                    args.architecture,
                                    args.workDir,
                                    args.insecure)
    elif args.remoteStore:
        syncHelper = RsyncRemoteSync(args.remoteStore,
                                     args.writeStore,
                                     args.architecture,
                                     args.workDir,
                                     "")
    else:
        syncHelper = NoRemoteSync()

    packages = args.pkgname
    dockerImage = args.dockerImage if "dockerImage" in args else None
    specs = {}
    buildOrder = []
    workDir = abspath(args.workDir)
    prunePaths(workDir)

    if not exists(args.configDir):
        msg = (
            "Cannot find {star}dist recipes under directory \"{cfg_dir}\".\n"
            "Maybe you need to \"cd\" to the right directory or "
            "you forgot to run \"aliBuild init\"?"
        ).format(star=star(), cfg_dir=args.configDir)
        return (error, msg, 1)

    def defaultsReader():
        return readDefaults(args.configDir, args.defaults, parser.error)

    (err, overrides, taps) = parseDefaults(args.disable, defaultsReader, debug)
    dieOnError(err, err)

    specDir = "%s/SPECS" % workDir
    if not exists(specDir):
        makedirs(specDir)

    os.environ["ALIBUILD_ALIDIST_HASH"] = getDirectoryHash(args.configDir)

    debug("Building for architecture %s" % args.architecture)
    debug("Number of parallel builds: %d" % args.jobs)
    msg = (
        "Using {star}Build from {star}build@{toolHash} recipes "
        "in {star}dist@{distHash}"
    ).format(
        star=star(),
        toolHash=getDirectoryHash(dirname(__file__)),
        distHash=os.environ["ALIBUILD_ALIDIST_HASH"]
    )
    debug(msg)

    def performPreferCheck(pkg, cmd):
        return dockerStatusOutput(cmd,
                                  dockerImage,
                                  executor=getStatusOutputBash)

    def performRequirementCheck(pkg, cmd):
        return dockerStatusOutput(cmd,
                                  dockerImage,
                                  executor=getStatusOutputBash)

    def performValidateDefaults(spec):
        return validateDefaults(spec, args.defaults)

    (systemPackages, ownPackages, failed, validDefaults) = \
        getPackageList(
            packages=packages,
            specs=specs,
            configDir=args.configDir,
            preferSystem=args.preferSystem,
            noSystem=args.noSystem,
            architecture=args.architecture,
            disable=args.disable,
            defaults=args.defaults,
            dieOnError=dieOnError,
            performPreferCheck=performPreferCheck,
            performRequirementCheck=performRequirementCheck,
            performValidateDefaults=performValidateDefaults,
            overrides=overrides,
            taps=taps,
            log=debug)

    if validDefaults and args.defaults not in validDefaults:
        validDefs = "\n- ".join(sorted(validDefaults))
        msg = (
            "Specified default `{default}' is not compatible with the "
            "packages you want to build.\nValid defaults:\n\n- {valid}"
        ).format(default=args.defaults, valid=validDefs)
        return (error, msg, 1)

    if failed:
        msg = (
            "The following packages are system requirements and "
            "could not be found:\n\n- {failed_pkgs}\n\n"
            "Please run:\n\n\t"
            "aliDoctor {pkg_name}\n\n"
            "to get a full diagnosis."
        ).format(
            pkg_name=args.pkgname.pop(),
            failed_pkgs="\n- ".join(sorted(list(failed)))
        )
        return (error, msg, 1)

    for x in specs.values():
        x["requires"] = [r for r in x["requires"] if r not in args.disable]
        x["build_requires"] = [
            r for r in x["build_requires"] if r not in args.disable
        ]
        x["runtime_requires"] = [
            r for r in x["runtime_requires"] if r not in args.disable
        ]

    if systemPackages:
        msg = ("{star}Build can take the following packages from "
               "the system and will not build them:\n {system}")
        banner(msg.format(star=star(), system=", ".join(systemPackages)))

    if ownPackages:
        msg = ("The following packages cannot be taken from the "
               "system and will be built:\n {own_pkgs}")
        banner(msg.format(own_pkgs=", ".join(ownPackages)))

    # Do topological sort to have the correct build order even in the
    # case of non-tree like dependencies..
    # The actual algorith used can be found at:
    #   http://www.stoimen.com/blog/2012/10/01/computer-algorithms-topological-sort-of-a-graph/
    #
    edges = [(p["package"], d) for p in specs.values() for d in p["requires"]]
    L = [l for l in specs.values() if not l["requires"]]
    S = []
    while L:
        spec = L.pop(0)
        S.append(spec)
        nextVertex = [e[0] for e in edges if e[1] == spec["package"]]
        edges = [e for e in edges if e[1] != spec["package"]]
        hasPredecessors = set(
            [m for e in edges for m in nextVertex if e[0] == m]
        )
        withPredecessor = set(nextVertex) - hasPredecessors
        L += [specs[m] for m in withPredecessor]

    buildOrder = [s["package"] for s in S]

    # Date fields to substitute: they are zero-padded
    now = datetime.now()
    nowKwds = {
        "year": str(now.year),
        "month": str(now.month).zfill(2),
        "day": str(now.day).zfill(2),
        "hour": str(now.hour).zfill(2)
    }

    # Check if any of the packages can be picked up from a local checkout
    develCandidates = [basename(d) for d in glob("*") if os.path.isdir(d)]
    develCandidatesUpper = [
        basename(d).upper() for d in glob("*") if os.path.isdir(d)
    ]
    develPkgs = [
        p for p in buildOrder if p in develCandidates and p not in args.noDevel
    ]
    develPkgsUpper = [
        (p, p.upper()) for p in buildOrder
        if p.upper() in develCandidatesUpper and p not in args.noDevel
    ]

    if set(develPkgs) != set(x for (x, y) in develPkgsUpper):
        bad_pkgs = ", ".join(
            set(x.strip() for (x, y) in develPkgsUpper) - set(develPkgs)
        )
        msg = (
            "The following development packages have the "
            "wrong spelling: {pkgs}.\n"
            "Please check your local checkout and adapt "
            "to the correct one indicated."
        ).format(pkgs=bad_pkgs)
        return (error, msg, 1)

    if buildOrder:
        pkg_order = "\n - ".join([
             x + " (development package)"
             if x in develPkgs
             else "%s@%s" % (x, specs[x]["tag"])
             for x in buildOrder if x != "defaults-release"
        ])
        msg = (
            "Packages will be built in the following order:\n - {pkgs}"
        ).format(pkgs=pkg_order)
        banner(msg)

    if develPkgs:
        msg = (
            "You have packages in development mode.\n"
            "This means their source code can be freely modified under:\n\n"
            "  {pwd}/<package_name>\n\n"
            "{star}Build does not automatically update such "
            "packages to avoid work loss.\n"
            "In most cases this is achieved by doing in the "
            " package source directory:\n\n"
            "  git pull --rebase\n"
        ).format(pwd=os.getcwd(), star=star())
        banner(msg)

    # Clone/update repos
    for p in [p for p in buildOrder if "source" in specs[p]]:
        updateReferenceRepoSpec(args.referenceSources,
                                p,
                                specs[p],
                                args.fetchRepos)

        # Retrieve git heads
        if "reference" in specs[p]:
            ref_src = specs[p]["reference"]
        else:
            ref_src = specs[p]["source"]
        cmd = "git ls-remote --heads {ref}".format(ref=ref_src)

        if specs[p]["package"] in develPkgs:
            specs[p]["source"] = join(os.getcwd(), specs[p]["package"])
            cmd = "git ls-remote --heads %s" % specs[p]["source"]

        debug("Executing %s" % cmd)
        res, output = getStatusOutputBash(cmd)
        dieOnError(res, "Error on '%s': %s" % (cmd, output))
        specs[p]["git_heads"] = output.split("\n")

    # Resolve the tag to the actual commit ref
    for p in buildOrder:
        spec = specs[p]
        spec["commit_hash"] = "0"
        develPackageBranch = ""

        if "source" in spec:
            # Tag may contain date params like:
            #    %(year)s, %(month)s, %(day)s, %(hour).
            spec["tag"] = format(spec["tag"], **nowKwds)

            # By default we assume tag is a commit hash. We then try to find
            # out if the tag is actually a branch and we use the tip of the
            # branch as commit_hash. Finally if the package is a development
            # one, we use the name of the branch as commit_hash.
            spec["commit_hash"] = spec["tag"]

            for head in spec["git_heads"]:
                head_suffix = "refs/heads/{0}".format(spec["tag"])
                if head.endswith(head_suffix) or spec["package"] in develPkgs:
                    spec["commit_hash"] = head.split("\t", 1)[0]
                    # We are in development mode, we need to rebuild if
                    # the commit hash is different and if there are extra
                    # changes on to.
                    if spec["package"] in develPkgs:
                        # Devel package: we get the commit hash from
                        # the checked source, not from remote.
                        cmd = "cd %s && git rev-parse HEAD" % spec["source"]
                        err, out = getstatusoutput(cmd)
                        dieOnError(err,
                                   "Unable to detect current commit hash.")

                        spec["commit_hash"] = out.strip()
                        cmd = (
                            "cd {src} && "
                            "git diff -r HEAD && "
                            "git status --porcelain"
                        ).format(src=spec["source"])

                        h = Hasher()
                        err = execute(cmd, h)
                        debug(err, cmd)
                        dieOnError(err,
                                   "Unable to detect source code changes.")

                        spec["devel_hash"] = spec["commit_hash"] + h.hexdigest()
                        cmd = (
                            "cd {src} && "
                            "git rev-parse --abbrev-ref HEAD"
                        ).format(src=spec["source"])
                        err, out = getstatusoutput(cmd)
                        if out == "HEAD":
                            cmd = "cd {src} && git rev-parse HEAD".format(
                                src=spec["source"]
                            )
                            err, out = getstatusoutput(cmd)
                            out = out[0:10]

                        if err:
                            msg = (
                                "Error, unable to lookup changes "
                                "in development package {pkg}. "
                                "Is it a git clone?"
                            ).format(pkg=spec["source"])
                            return (error, msg, 1)

                        develPackageBranch = out.replace("/", "-")
                        if "develPrefix" in args:
                            spec["tag"] = args.develPrefix
                        else:
                            spec["tag"] = develPackageBranch

                        spec["commit_hash"] = "0"
                    break

        # Version may contain date params like tag,
        # plus %(commit_hash)s, %(short_hash)s and %(tag)s.
        defaults_upper = ""
        if args.defaults != "release":
            defaults_upper = "_" + args.defaults.upper().replace("-", "_")

        spec["version"] = format(spec["version"],
                                 commit_hash=spec["commit_hash"],
                                 short_hash=spec["commit_hash"][0:10],
                                 tag=spec["tag"],
                                 tag_basename=basename(spec["tag"]),
                                 defaults_upper=defaults_upper,
                                 **nowKwds)

        if (
            spec["package"] in develPkgs and
            "develPrefix" in args and
            args.develPrefix != "ali-master"
        ):
            spec["version"] = args.develPrefix

    # Decide what is the main package we are building and at what commit.
    # We emit an event for the main package, when encountered, so that
    # we can use it to index builds of the same hash on different
    # architectures. We also make sure add the main package and it's hash
    # to the debug log, so that we can always extract it from it.
    # If one of the special packages is in the list of packages to be built,
    # we use it as main package, rather than the last one.
    if not buildOrder:
        return (banner, "Nothing to be done.", 0)

    mainPackage = buildOrder[-1]
    mainHash = specs[mainPackage]["commit_hash"]

    debug("Main package is %s@%s" % (mainPackage, mainHash))
    if args.debug:
        dp = args.develPrefix if "develPrefix" in args else mainHash[0:8]
        fmt_str = "%%(levelname)s:{main}:{dp}: %%(message)s".format(
            main=mainPackage, dp=dp
        )
        logger_handler.setFormatter(LogFormatter(fmt_str))

    # Now that we have the main package set, we can print out
    # useful information which we will be able to associate with
    # this build. Also lets make sure each package we need to build
    # can be built with the current default.
    for p in buildOrder:
        spec = specs[p]
        if "source" in spec:
            msg = "Commit hash for {src}@{tag} is {hash}".format(
                src=spec["source"], tag=spec["tag"], hash=spec["commit_hash"]
            )
            debug(msg)

    # Calculate the hashes. We do this in build order so that we
    # can guarantee that the hashes of the dependencies are calculated first.
    # Also notice that if the commit hash is a real hash, and not a tag,
    # we can safely assume that's unique, and therefore we can avoid putting
    # the repository or the name of the branch in the hash.
    debug("Calculating hashes.")
    for p in buildOrder:
        spec = specs[p]
        debug(spec)
        debug(develPkgs)
        h = Hasher()
        dh = Hasher()
        for x in ["recipe", "version", "package", "commit_hash",
                  "env", "append_path", "prepend_path"]:
            if (
                sys.version_info[0] < 3 and
                x in spec and
                type(spec[x]) == OrderedDict
            ):
                # Python 2: use YAML dict order to prevent changing hashes
                h(str(yaml.safe_load(yamlDump(spec[x]))))
            else:
                h(str(spec.get(x, "none")))

        if spec["commit_hash"] == spec.get("tag", "0"):
            h(spec.get("source", "none"))
            if "source" in spec:
                h(spec["tag"])

        for dep in spec.get("requires", []):
            h(specs[dep]["hash"])
            dh(specs[dep]["hash"] + specs[dep].get("devel_hash", ""))

        if bool(spec.get("force_rebuild", False)):
            h(str(time.time()))
        if spec["package"] in develPkgs and "incremental_recipe" in spec:
            h(spec["incremental_recipe"])
            ih = Hasher()
            ih(spec["incremental_recipe"])
            spec["incremental_hash"] = ih.hexdigest()
        elif p in develPkgs:
            h(spec.get("devel_hash"))

        if args.architecture.startswith("osx") and "relocate_paths" in spec:
            h("relocate:"+" ".join(sorted(spec["relocate_paths"])))

        spec["hash"] = h.hexdigest()
        spec["deps_hash"] = dh.hexdigest()
        debug("Hash for recipe %s is %s" % (p, spec["hash"]))

    # This adds to the spec where it should find, locally or remotely
    # the various tarballs and links.
    for p in buildOrder:
        spec = specs[p]
        pkgSpec = {
            "workDir": workDir,
            "package": spec["package"],
            "version": spec["version"],
            "hash": spec["hash"],
            "prefix": spec["hash"][0:2],
            "architecture": args.architecture
        }

        varSpecs = [
            ("storePath", "TARS/%(architecture)s/store/%(prefix)s/%(hash)s"),
            ("linksPath", "TARS/%(architecture)s/%(package)s"),
            ("tarballHashDir", "%(workDir)s/TARS/%(architecture)s/store/%(prefix)s/%(hash)s"),
            ("tarballLinkDir", "%(workDir)s/TARS/%(architecture)s/%(package)s"),
            ("buildDir", "%(workDir)s/BUILD/%(hash)s/%(package)s")
        ]
        spec.update(dict([(x, format(y, **pkgSpec)) for (x, y) in varSpecs]))
        hash_file = spec["buildDir"] + "/.build_succeeded"
        spec["old_devel_hash"] = readHashFile(hash_file)

    # We recursively calculate the full set of requires "full_requires"
    # including build_requires and the subset of them which are needed at
    # runtime "full_runtime_requires".
    for p in buildOrder:
        spec = specs[p]
        todo = [p]
        spec["full_requires"] = []
        spec["full_runtime_requires"] = []
        while todo:
            i = todo.pop(0)
            requires = specs[i].get("requires", [])
            runTimeRequires = specs[i].get("runtime_requires", [])
            spec["full_requires"] += requires
            spec["full_runtime_requires"] += runTimeRequires
            todo += requires
        spec["full_requires"] = set(spec["full_requires"])
        spec["full_runtime_requires"] = set(spec["full_runtime_requires"])

    build_order = " ".join(buildOrder)
    debug("We will build packages in the following order: %s" % build_order)
    if args.dryRun:
        return (info, "--dry-run / -n specified. Not building.", 0)

    # We now iterate on all the packages, making sure we build correctly
    # every single one of them. This is done this way so that the second
    # time we run we can check if the build was consistent and if it is,
    # we bail out.
    packageIterations = 0
    tpl = "{p} disabled={dis} devel={dev} system={sys} own={own} deps={deps}"
    action = tpl.format(p=args.pkgname,
                        dis=",".join(sorted(args.disable)),
                        dev=",".join(sorted(develPkgs)),
                        sys=",".join(sorted(systemPackages)),
                        own=",".join(sorted(ownPackages)),
                        deps=",".join(buildOrder[:-1]))
    report_event("install", action, args.architecture)

    while buildOrder:
        packageIterations += 1
        if packageIterations > 20:
            msg = (
                "Too many attempts at building {package}. "
                "Something wrong with the repository?"
            ).format(package=spec["package"])
            return (error, msg, 1)

        p = buildOrder[0]
        spec = specs[p]
        if args.debug:
            dp = args.develPrefix if "develPrefix" in args else mainHash[0:8]
            fmt_str = "%%(levelname)s:{main}:{p}:{dp}: %%(message)s".format(
                main=mainPackage, p=p, dp=dp
            )
            logger_handler.setFormatter(LogFormatter(fmt_str))

        if (
            spec["package"] in develPkgs and
            getattr(syncHelper, "writeStore", None)
        ):
            msg = ("Disabling remote write store from now since "
                   "{package} is a development package.")
            warning(msg.format(package=spec["package"]))
            syncHelper.writeStore = ""

        # Since we can execute this multiple times for a given package,
        # in order to ensure consistency, we need to reset things and
        # make them pristine.
        spec.pop("revision", None)
        riemannStream.setAttributes(package=spec["package"],
                                    package_hash=spec["version"],
                                    architecture=args.architecture,
                                    defaults=args.defaults)
        riemannStream.setState("warning")

        debug("Updating from tarballs")
        # If we arrived here it really means we have a tarball which
        # was created using the same recipe. We will use it as a cache
        # for the build. This means that while we will still perform
        # the build process, rather than executing the build itself we will:
        #    - Unpack it in a temporary place.
        #    - Invoke the relocation specifying the correct work_dir and the
        #      correct path which should have been used.
        #    - Move the version directory to its final destination,
        #      including the correct revision.
        #    - Repack it and put it in the store with the
        #
        # This will result in a new package which has the same binary
        # contents of the old one but where the relocation will work
        # for the new dictory. Here we simply store the fact that we
        # can reuse the contents of cachedTarball.
        syncHelper.syncToLocal(p, spec)

        # Decide how it should be called, based on the hash and what
        # is already available.
        debug("Checking for packages already built.")
        linksGlob = "{w}/TARS/{a}/{p}/{p}-{v}-*.{a}.tar.gz".format(
            w=workDir,
            a=args.architecture,
            p=spec["package"],
            v=spec["version"]
        )
        debug("Glob pattern used: %s" % linksGlob)
        packages = glob(linksGlob)

        # In case there is no installed software, revision is 1
        # If there is already an installed package:
        #   - Remove it if we do not know its hash
        #   - Use the latest number in the version, to decide its revision
        debug("Packages already built using this version\n{0}".format(
            "\n".join(packages)
        ))
        busyRevisions = []

        # Calculate the build_family for the package
        # If the package is a devel package, we need to associate
        # it a devel prefix, either via the -z option or using its
        # checked out branch. This affects its build hash.
        #
        # Moreover we need to define a global "buildFamily" which
        # is used to tag all the packages incurred in the build,
        # this way we can have a latest-<buildFamily> link for all
        # of them an we will not incur in the flip - flopping described in
        #     https://github.com/alisw/alibuild/issues/325.
        develPrefix = ""
        possibleDevelPrefix = getattr(args, "develPrefix", develPackageBranch)
        if spec["package"] in develPkgs:
            develPrefix = possibleDevelPrefix

        if possibleDevelPrefix:
            spec["build_family"] = possibleDevelPrefix + "-" + args.defaults
        else:
            spec["build_family"] = args.defaults

        if spec["package"] == mainPackage:
            mainBuildFamily = spec["build_family"]

        for d in packages:
            realPath = readlink(d)
            matcher = (
                "../../{a}/store/[0-9a-f]{{2}}/"
                "([0-9a-f]*)/{p}-{v}-([0-9]*).{a}.tar.gz$"
            ).format(
                a=args.architecture,
                p=spec["package"],
                v=spec["version"]
            )

            m = re.match(matcher, realPath)
            if not m:
                continue
            h, revision = m.groups()
            revision = int(revision)

            # If we have an hash match, we use the old revision
            # for the package and we do not need to build it.
            if h != spec["hash"]:
                busyRevisions.append(revision)
            else:
                spec["revision"] = revision
                if spec["package"] in develPkgs and "incremental_recipe" in spec:
                    spec["obsolete_tarball"] = d
                else:
                    debug(
                        ("Package {0} with hash {1} is already found in {2}. "
                         "Not building.").format(p, h, d)
                    )
                    src = spec["version"] + "-" + spec["revision"]

                    dst1 = "{w}/{a}/{p}/latest-{bf}".format(
                        w=workDir,
                        a=args.architecture,
                        p=spec["package"],
                        bf=spec["build_family"]
                    )
                    dst2 = "{w}/{a}/{p}/latest".format(
                        w=workDir,
                        a=args.architecture,
                        p=spec["package"]
                    )

                    getstatusoutput("ln -snf %s %s" % (src, dst1))
                    getstatusoutput("ln -snf %s %s" % (src, dst2))
                    info("Using cached build for %s" % p)
                break

        if "revision" not in spec and busyRevisions:
            rd = min(set(range(1, max(busyRevisions)+2)) - set(busyRevisions))
            spec["revision"] = rd
        elif "revision" in spec:
            spec["revision"] = "1"

        # Check if this development package needs to be rebuilt.
        if spec["package"] in develPkgs:
            msg = "Checking if devel package {p} needs rebuild"
            debug(msg.format(p=spec["package"]))

            if spec["devel_hash"] + spec["deps_hash"] == spec["old_devel_hash"]:
                msg = "Development package {p} does not need rebuild"
                info(msg.format(p=spec["package"]))
                buildOrder.pop(0)
                continue

        # Now that we have all the information about the package
        # we want to build, let's check if it wasn't built / unpacked already.
        hashFile = "%s/%s/%s/%s-%s/.build-hash" % (workDir,
                                                   args.architecture,
                                                   spec["package"],
                                                   spec["version"],
                                                   spec["revision"])
        fileHash = readHashFile(hashFile)
        if fileHash != spec["hash"]:
            if fileHash != "0":
                msg = (
                    "Mismatch between local area ({file_hash}) "
                    "and the one which I should build ({hash}). Redoing."
                ).format(file_hash=fileHash, hash=spec["hash"])
                debug(msg)
                shutil.rmtree(dirname(hashFile), True)
        else:
            # If we get here, we know we are in sync with whatever
            # remote store. We can therefore create a directory which
            # contains all the packages which were used to compile this one.
            riemannStream.setState('ok')
            msg = "Package {p} was correctly compiled. Moving to next one."
            debug(msg.format(p=spec["package"]))

            # If using incremental builds, next time we execute the
            # script we need to remove the placeholders which avoid rebuilds.
            if spec["package"] in develPkgs and "incremental_recipe" in spec:
                unlink(hashFile)
            if "obsolete_tarball" in spec:
                unlink(realpath(spec["obsolete_tarball"]))
                unlink(spec["obsolete_tarball"])

            # We need to create 2 sets of links, once with the
            # full requires, once with only direct dependencies,
            # since that's required to register packages in Alien.
            createDistLinks(spec, specs, args, "dist", "full_requires")
            createDistLinks(spec, specs, args, "dist-direct", "requires")
            createDistLinks(spec, specs, args,
                            "dist-runtime", "full_runtime_requires")
            buildOrder.pop(0)
            packageIterations = 0

            # We can now delete the INSTALLROOT and BUILD directories,
            # assuming the package is not a development one. We also can
            # delete the SOURCES in case we have aggressive-cleanup enabled.
            if not spec["package"] in develPkgs and args.autoCleanup:
                cleanupDirs = [
                    "{w}/BUILD/{h}".format(w=workDir, h=spec["hash"]),
                    "{w}/INSTALLROOT/{h}".format(w=workDir, h=spec["hash"])
                ]

                if args.aggressiveCleanup:
                    cleanupDirs.append(
                        "{w}/SOURCES/{p}".format(w=workDir, p=spec["package"])
                    )

                debug("Cleaning up:\n" + "\n".join(cleanupDirs))

                for d in cleanupDirs:
                    shutil.rmtree(d.encode("utf8"), True)

                try:
                    unpath = "{w}/BUILD/{p}-latest".format(w=workDir,
                                                           p=spec["package"])
                    unlink(unpath)

                    if "develPrefix" in args:
                        unpath = "{w}/BUILD/{p}-latest-{dp}".format(
                            w=workDir,
                            p=spec["package"],
                            dp=args.develPrefix
                        )
                        unlink(unpath)
                except:
                    pass

                try:
                    rmdir(format("%(w)s/BUILD", w=workDir, p=spec["package"]))
                    rmdir(format("%(w)s/INSTALLROOT", w=workDir, p=spec["package"]))
                except:
                    pass
            continue

        debug("Looking for cached tarball in %s" % spec["tarballHashDir"])
        # FIXME: I should get the tarballHashDir updated with server
        # at this point. It does not really matter that the symlinks
        # are ok at this point as I only used the tarballs as reusable
        # binary blobs.
        spec["cachedTarball"] = ""
        if not spec["package"] in develPkgs:
            tarballs = [
                x for x in glob("%s/*" % spec["tarballHashDir"])
                if x.endswith("gz")
            ]
            spec["cachedTarball"] = tarballs[0] if len(tarballs) else ""
            debug(spec["cachedTarball"] and
                  "Found tarball in %s" % spec["cachedTarball"] or
                  "No cache tarballs found")

        # Generate the part which sources the environment for all
        # the dependencies. Notice that we guarantee that a dependency
        # is always sourced before the parts depending on it, but we do
        # not guaranteed anything for the order in which unrelated
        # components are activated.
        dependencies = ""
        dependenciesInit = ""
        for package in spec.get("requires", []):
            architecture = args.architecture
            version = specs[package]["version"]
            revision = specs[package]["revision"]
            version_rev = version + "-" + revision
            bigpackage = package.upper().replace("-", "_")

            init_file = join(
                "$WORK_DIR",
                architecture,
                package,
                version_rev,
                "etc/profile.d/init.sh"
            )

            deps = (
                "[ \"X${bigpackage}_VERSION\" = X  ] && "
                "source \"{init_file}\"\n"
            ).format(bigpackage=bigpackage, init_file=init_file)
            dependencies += deps

            deps = (
                'echo [ \\\"X\${bigpackage}_VERSION\\\" = X ] \&\& '
                'source \{init_file} >> '
                '\"$INSTALLROOT/etc/profile.d/init.sh\"\n'
            ).format(bigpackage=bigpackage, init_file=init_file)
            dependenciesInit += deps

        # Generate the part which creates the environment for the package.
        # This can be either variable set via the "env" keyword in the metadata
        # or paths which get appended via the "append_path" one.
        # By default we append LD_LIBRARY_PATH, PATH and DYLD_LIBRARY_PATH
        # FIXME: do not append variables for Mac on Linux.
        environment = ""
        dieOnError(not isinstance(spec.get("env", {}), dict),
                   "Tag `env' in %s should be a dict." % p)

        for key, value in spec.get("env", {}).items():
            environment += (
                "echo 'export {key}=\"{value}\"' >> "
                "$INSTALLROOT/etc/profile.d/init.sh\n"
            ).format(key=key, value=value)

        basePath = "%s_ROOT" % p.upper().replace("-", "_")

        pathDict = spec.get("append_path", {})
        dieOnError(not isinstance(pathDict, dict),
                   "Tag `append_path' in %s should be a dict." % p)

        for pathName, pathVal in pathDict.items():
            pathVal = isinstance(pathVal, list) and pathVal or [pathVal]
            environment += (
                "\ncat << \EOF >> \"$INSTALLROOT/etc/profile.d/init.sh\"\n"
                "export {key}=${key}:{value}\n"
                "EOF"
            ).format(key=pathName, value=":".join(pathVal))

        # Same thing, but prepending the results so that
        # they win against system ones.
        defaultPrependPaths = {
            "LD_LIBRARY_PATH": "$%s/lib" % basePath,
            "DYLD_LIBRARY_PATH": "$%s/lib" % basePath,
            "PATH": "$%s/bin" % basePath
        }

        pathDict = spec.get("prepend_path", {})
        dieOnError(not isinstance(pathDict, dict),
                   "Tag `prepend_path' in %s should be a dict." % p)

        for pathName, pathVal in pathDict.items():
            pathDict[pathName] = (
                isinstance(pathVal, list) and pathVal or [pathVal]
            )

        for pathName, pathVal in defaultPrependPaths.items():
            pathDict[pathName] = [pathVal] + pathDict.get(pathName, [])

        for pathName, pathVal in pathDict.items():
            environment += (
                "\ncat << "
                "\EOF >> "
                "\"$INSTALLROOT/etc/profile.d/init.sh\"\n"
                "export {key}={value}:${key}\n"
                "EOF"
            ).format(key=pathName, value=":".join(pathVal))

        # The actual build script.
        referenceStatement = ""
        if "reference" in spec:
            referenceStatement = (
                "export GIT_REFERENCE=${GIT_REFERENCE_OVERRIDE:-%s}/%s"
            ) % (dirname(spec["reference"]), basename(spec["reference"]))

        debug(spec)

        cmd_raw = ""
        try:
            template = join(dirname(realpath(__file__)),
                            "alibuild_helpers/build_template.sh")
            fp = open(template, 'r')
            cmd_raw = fp.read()
            fp.close()
        except:
            from pkg_resources import resource_string
            cmd_raw = resource_string("alibuild_helpers", 'build_template.sh')

        source = spec.get("source", "")

        # Shortend the commit hash in case it's a real commit hash
        # and not simply the tag.
        commit_hash = spec["commit_hash"]
        if spec["tag"] != spec["commit_hash"]:
            commit_hash = spec["commit_hash"][0:10]

        # Split the source in two parts, sourceDir and sourceName.
        # This is done so that when we use Docker we can replace
        # sourceDir with the correct container path, if required.
        # No changes for what concerns the standard bash builds, though.
        if args.docker:
            cachedTarball = re.sub("^" + workDir, "/sw", spec["cachedTarball"])
        else:
            cachedTarball = spec["cachedTarball"]

        cmd = cmd_raw.format(
            dependencies=dependencies,
            dependenciesInit=dependenciesInit,
            develPrefix=develPrefix,
            environment=environment,
            workDir=workDir,
            configDir=abspath(args.configDir),
            incremental_recipe=spec.get("incremental_recipe", ":"),
            sourceDir=source and (dirname(source) + "/") or "",
            sourceName=source and basename(source) or "",
            referenceStatement=referenceStatement,
            requires=" ".join(spec["requires"]),
            build_requires=" ".join(spec["build_requires"]),
            runtime_requires=" ".join(spec["runtime_requires"])
        )

        commonPath = "%s/%%s/%s/%s/%s-%s" % (workDir,
                                             args.architecture,
                                             spec["package"],
                                             spec["version"],
                                             spec["revision"])
        scriptDir = commonPath % "SPECS"

        err, out = getstatusoutput("mkdir -p %s" % scriptDir)
        writeAll("%s/build.sh" % scriptDir, cmd)
        writeAll("%s/%s.sh" % (scriptDir, spec["package"]), spec["recipe"])

        suffix = spec["version"]
        if "develPrefix" in args and spec["package"] in develPkgs:
            suffix = args.develPrefix
        banner("Building " + spec["package"] + "@" + suffix)

        # Define the environment so that it can be passed up to the
        # actual build script
        buildEnvironment = [
            ("ARCHITECTURE", args.architecture),
            ("BUILD_REQUIRES", " ".join(spec["build_requires"])),
            ("CACHED_TARBALL", cachedTarball),
            ("CAN_DELETE", args.aggressiveCleanup and "1" or ""),
            ("COMMIT_HASH", commit_hash),
            ("DEPS_HASH", spec.get("deps_hash", "")),
            ("DEVEL_HASH", spec.get("devel_hash", "")),
            ("DEVEL_PREFIX", develPrefix),
            ("BUILD_FAMILY", spec["build_family"]),
            ("GIT_TAG", spec["tag"]),
            ("MY_GZIP", gzip()),
            ("MY_TAR", tar()),
            ("INCREMENTAL_BUILD_HASH", spec.get("incremental_hash", "0")),
            ("JOBS", args.jobs),
            ("PKGHASH", spec["hash"]),
            ("PKGNAME", spec["package"]),
            ("PKGREVISION", spec["revision"]),
            ("PKGVERSION", spec["version"]),
            ("RELOCATE_PATHS", " ".join(spec.get("relocate_paths", []))),
            ("REQUIRES", " ".join(spec["requires"])),
            ("RUNTIME_REQUIRES", " ".join(spec["runtime_requires"])),
            ("WRITE_REPO", spec.get("write_repo", source)),
        ]

        # Add the extra environment as passed from the command line.
        for e in [x.split("=", "1") for x in args.environment]:
            buildEnvironment.append(e)

        # In case the --docker options is passed, we setup a docker
        # container which will perform the actual build. Otherwise
        # build as usual using bash.
        if args.docker:
            additionalEnv = ""
            additionalVolumes = ""
            develVolumes = ""
            mirrorVolume = ""
            if "reference" in spec:
                mirrorVolume = " -v %s:/mirror" % dirname(spec["reference"])

            overrideSource = ""
            if source.startswith("/"):
                overrideSource = "-e SOURCE0_DIR_OVERRIDE=/"

            for devel in develPkgs:
                dv = (" -v $PWD/`readlink {0} || "
                      "echo {0}`:/{0}:ro ").format(devel)
                develVolumes += dv

            for env in buildEnvironment:
                additionalEnv += " -e %s='%s' " % env
            for volume in args.volumes:
                additionalVolumes += " -v %s " % volume

            dockerWrapper = (
                "docker run --rm -it"
                " -v {workdir}:/sw"
                " -v {scriptDir}/build.sh:/build.sh:ro"
                " mirrorVolume}"
                " {develVolumes}"
                " {additionalEnv}"
                " {additionalVolumes}"
                " -e GIT_REFERENCE_OVERRIDE=/mirror"
                " {overrideSource)s"
                " -e WORK_DIR_OVERRIDE=/sw"
                " {image}"
                " {bash} -e -x /build.sh"
            ).format(
                additionalEnv=additionalEnv,
                additionalVolumes=additionalVolumes,
                bash=BASH,
                develVolumes=develVolumes,
                workdir=abspath(args.workDir),
                image=dockerImage,
                mirrorVolume=mirrorVolume,
                overrideSource=overrideSource,
                scriptDir=scriptDir
            )

            debug(dockerWrapper)
            err = execute(dockerWrapper)
        else:
            msg = ("{package} is being built (use "
                   "--debug for full output)").format(package=spec["package"])
            progress = ProgressPrint(msg)

            for k, v in buildEnvironment:
                os.environ[k] = str(v)

            cmd = "%s -e -x %s/build.sh 2>&1" % (BASH, scriptDir)
            printer = (debug
                       if args.debug or not sys.stdout.isatty()
                       else progress)
            err = execute(cmd, printer=printer)
            progress.end("failed" if err else "ok", err)

        report_event(
            "BuildError" if err else "BuildSuccess",
            spec["package"],
            " ".join([
                args.architecture,
                spec["version"],
                spec["commit_hash"],
                os.environ["ALIBUILD_ALIDIST_HASH"][0:10]
            ])
        )

        updatablePkgs = [x for x in spec["requires"] if x in develPkgs]
        if spec["package"] in develPkgs:
            updatablePkgs.append(spec["package"])

        buildErrMsg = (
            "Error while executing {sd}/build.sh on `{h}'.\n"
            "Log can be found in {w}/BUILD/{p}-latest{devSuffix}/log.\n"
            "Please upload to CERNBox or DropBox before requesting support.\n"
            "Build directory is {w}/BUILD/{p}-latest{devSuffix}/{p}."
        ).format(
            h=socket.gethostname(),
            sd=scriptDir,
            w=abspath(args.workDir),
            p=spec["package"],
            devSuffix="-" + args.develPrefix
            if "develPrefix" in args and spec["package"] in develPkgs else ""
        )

        if updatablePkgs:
            cmd = "".join([
                "\n  ( cd %s && git pull --rebase )" % dp
                for dp in updatablePkgs
            ])
            buildErrMsg += (
                "\n\nNote that you have packages in development mode.\n"
                "Devel sources are not updated automatically, you must "
                "do it by hand.\nThis problem might be due to one or more "
                "outdated devel sources.\nTo update all development "
                "packages required for this build it is usually sufficient "
                "to do:\n{updatablePkgs}"
            ).format(updatablePkgs=cmd)

        dieOnError(err, buildErrMsg)

        syncHelper.syncToRemote(p, spec)

    msg = (
        "Build of {mainPackage} successfully completed on `{h}'.\n"
        "Your software installation is at:"
        "\n\n  {wp}\n\n"
        "You can use this package by loading the environment:"
        "\n\n  alienv enter {mainPackage}/latest-{buildFamily}"
    ).format(
        mainPackage=mainPackage,
        h=socket.gethostname(),
        wp=abspath(join(args.workDir, args.architecture)),
        buildFamily=mainBuildFamily
    )
    banner(msg)

    for x in develPkgs:
        msg = (
            "Build directory for devel package {p}:\n"
            "{w}/BUILD/{p}-latest{devSuffix}/{p}"
        ).format(
            p=x,
            devSuffix="-"+args.develPrefix if "develPrefix" in args else "",
            w=abspath(args.workDir)
        )

    return (debug, "Everything done", 0)

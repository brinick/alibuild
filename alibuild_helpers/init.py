from os.path import join, exists
import os

from alibuild_helpers.cmd import execute
from alibuild_helpers.utilities import (
    getPackageList, parseDefaults, readDefaults, validateDefaults
)

from alibuild_helpers.log import (
    debug, error, warning, banner, info, dieOnError
)

from alibuild_helpers.workarea import updateReferenceRepoSpec


def parsePackagesDefinition(pkgname):
    packages = [x + "@" for x in list(filter(lambda y: y, pkgname.split(",")))]
    return [dict(zip(["name", "ver"], y.split("@")[0:2])) for y in packages]


def doInit(args):
    assert(args.pkgname is not None)
    assert(type(args.dist) == dict)
    assert(sorted(args.dist.keys()) == ["repo", "ver"])
    pkgs = parsePackagesDefinition(args.pkgname)
    assert(type(pkgs) == list)
    if args.dryRun:
        pkg_names = ",".join(x["name"] for x in pkgs)
        msg = (
            "This will initialise local checkouts for {names}\n"
            "--dry-run / -n specified. Doing nothing."
        ).format(names=pkg_names)
        info(msg)
        exit(0)

    try:
        exists(args.develPrefix) or os.mkdir(args.develPrefix)
        exists(args.referenceSources) or os.makedirs(args.referenceSources)
    except OSError as e:
        error(str(e))
        exit(1)

    # Fetch recipes first if necessary
    if exists(args.configDir):
        warning("using existing recipes from %s" % args.configDir)
    else:
        defaultRepo = "https://github.com/%s" % args.dist["repo"]
        repo = args.dist["repo"] if ":" in args.dist["repo"] else defaultRepo
        branch = " -b "+args.dist["ver"] if args.dist["ver"] else "",
        cd = args.configDir

        cmd = "git clone {repo}{branch} {cd}".format(
            repo=repo,
            branch=branch,
            cd=cd
        )

        debug(cmd)
        err = execute(cmd)
        dieOnError(err != 0, "cannot clone recipes")

    # Use standard functions supporting overrides and taps. Ignore all disables
    # and system packages as they are irrelevant in this context
    specs = {}

    def defaultsReader():
        return readDefaults(args.configDir, args.defaults, error)

    def doNothing(*a, **kws):
        return None

    def doPreferCheck(*a, **kws):
        return (1, "")

    def doReqCheck(*a, **kws):
        return (0, "")

    def doValidateDefaults(spec):
        return validateDefaults(spec, args.defaults)

    (err, overrides, taps) = parseDefaults([], defaultsReader, debug)
    (_, _, _, validDefaults) = getPackageList(
                                    packages=[p["name"] for p in pkgs],
                                    specs=specs,
                                    configDir=args.configDir,
                                    preferSystem=False,
                                    noSystem=True,
                                    architecture="",
                                    disable=[],
                                    defaults=args.defaults,
                                    dieOnError=doNothing,
                                    performPreferCheck=doPreferCheck,
                                    performRequirementCheck=doReqCheck,
                                    performValidateDefaults=doValidateDefaults,
                                    overrides=overrides,
                                    taps=taps,
                                    log=debug)

    dieCond = validDefaults and args.defaults not in validDefaults
    validDefaults = "\n-".join(validDefaults)
    dieMsg = ("Specified default `{default}' is not compatible with "
              "the packages you want to build.\nValid defaults:\n\n-{valid}")
    dieMsg = dieMsg.format(default=args.defaults, valid=validDefaults)
    dieOnError(dieCond, dieMsg)

    for p in pkgs:
        spec = specs.get(p["name"])
        dieOnError(
            spec is None,
            "cannot find recipe for package %s" % p["name"]
        )

        dest = join(args.develPrefix, spec["package"])
        writeRepo = spec.get("write_repo", spec.get("source"))
        dieOnError(
            not writeRepo,
            ("package {package} has no source field and "
             "cannot be developed".format(package=spec["package"]))
        )

        if exists(dest):
            warning("not cloning %s since it already exists" % spec["package"])
            continue

        p["ver"] = p["ver"] if p["ver"] else spec.get("tag", spec["version"])
        version = p["ver"] if p["ver"] else ""

        package_version = "{pkg} {vers}".format(pkg=spec["package"],
                                                vers="version " + version)
        msg = "cloning {pkg} for development".format(pkg=package_version)
        debug(msg)

        updateReferenceRepoSpec(args.referenceSources,
                                spec["package"],
                                spec,
                                True)
        cmd = (
            "git clone {read_repo}{branch} --reference {ref_src} {cd} && "
            "cd {cd} && "
            "git remote set-url --push origin {write_repo}"
        ).format(
            repo_repo=spec["source"],
            write_repo=writeRepo,
            branch=" -b " + version,
            ref_src=join(args.referenceSources, spec["package"].lower()),
            cd=dest
        )

        debug(cmd)
        err = execute(cmd)
        dieOnError(err != 0, "cannot clone {pkg}".format(pkg=package_version))

    packages = ""
    if pkgs:
        pkg_names = [x["name"].lower() for x in pkgs]
        packages = " for "+", ".join(pkg_names)

    bruce = "Development directory {devdir} created {pkgs}"
    bruce = bruce.format(devdir=args.develPrefix, pkgs=packages)
    banner(bruce)

#!/usr/bin/env python

try:
    from commands import getstatusoutput
except ImportError:
    from subprocess import getstatusoutput

from os.path import basename, dirname, exists

try:
    from collections import OrderedDict
except ImportError:
    from ordereddict import OrderedDict

import base64
import glob
import hashlib
import os
import re
import sys
import subprocess
import yaml


class SpecError(Exception):
    pass


def asList(x):
    return x if type(x) == list else [x]


def is_string(s):
    if sys.version_info[0] >= 3:
        return isinstance(s, str)
    return isinstance(s, basestring)


def to_unicode(s):
    if sys.version_info[0] >= 3:
        if isinstance(s, bytes):
            # to get newlines as such and not as escaped \n
            return s.decode("utf-8")
        return str(s)
    elif isinstance(s, str):
        return unicode(s, "utf-8")  # utf-8 is a safe assumption
    elif not isinstance(s, unicode):
        return unicode(str(s))
    return s


def normalise_multiple_options(option, sep=","):
    return [x for x in ",".join(option).split(sep) if x]


def prunePaths(workDir):
    for x in ["PATH", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"]:
        if x not in os.environ:
            continue
        workDirEscaped = re.escape("%s" % workDir) + "[^:]*:?"
        os.environ[x] = re.sub(workDirEscaped, "", os.environ[x])

    for x in list(os.environ.keys()):
        if x.endswith("_VERSION") and x != "ALIBUILD_VERSION":
            os.environ.pop(x)


def validateSpec(spec):
    if not spec:
        raise SpecError("Empty recipe.")
    if type(spec) != OrderedDict:
        raise SpecError("Not a YAML key / value.")
    if "package" not in spec:
        raise SpecError("Missing package field in header.")


def validateDefaults(finalPkgSpec, defaults):
    """Check if a given spec is compatible with the given default."""
    if "valid_defaults" not in finalPkgSpec:
        return (True, "", [])

    validDefaults = asList(finalPkgSpec["valid_defaults"])
    nonStringDefaults = [x for x in validDefaults if not type(x) == str]
    if nonStringDefaults:
        msg = ("valid_defaults needs to be a string or "
               "a list of strings. Found {0}.".format(nonStringDefaults))
        return (False, msg, [])

    if defaults in validDefaults:
        return (True, "", validDefaults)

    valid = "\n".join([" - " + x for x in validDefaults])
    msg = "Cannot compile {0} with `{1}' default. Valid defaults are:\n{2}"
    msg = msg.format(finalPkgSpec["package"], defaults, valid)
    return (False, msg, validDefaults)


def format(s, **kwds):
    return to_unicode(s) % kwds


def doDetectArch(hasOsRelease, osReleaseLines,
                 platformTuple, platformSystem, platformProcessor):
    if platformSystem == "Darwin":
        return "osx_x86-64"

    distribution, version, flavour = platformTuple
    # If platform.dist does not return something sensible,
    # let's try with /etc/os-release
    if distribution not in ["Ubuntu", "redhat", "centos"] and hasOsRelease:
        for x in osReleaseLines:
            if "=" not in x:
                continue
            key, val = x.split("=", 1)
            val = val.strip("\n \"")
            if key == "ID":
                distribution = val
            if key == "VERSION_ID":
                version = val

    if distribution.lower() == "ubuntu":
        version = version.split(".")
        version = version[0] + version[1]
    elif distribution.lower() == "debian":
        # http://askubuntu.com/questions/445487/which-ubuntu-version-is-equivalent-to-debian-squeeze
        debian_ubuntu = {"7": "1204", "8": "1404"}
        if version in debian_ubuntu:
            distribution = "ubuntu"
            version = debian_ubuntu[version]

    elif distribution in ["redhat", "centos"]:
        distribution = distribution.replace("centos", "slc")
        distribution = distribution.replace("redhat", "slc").lower()

    processor = platformProcessor
    if not processor:
        # Sometimes platform.processor returns an empty string
        p = subprocess.Popen(["uname", "-m"], stdout=subprocess.PIPE)
        processor = p.stdout.read().strip()

    return "{0}{1}_{2}".format(distribution.lower(),
                               version.split(".")[0],
                               processor.replace("_", "-"))


def detectArch():
    """Try to guess a good platform. This does not try to cover all the
    possibly compatible linux distributions, but tries to get right the
    common one, obvious one. If you use a Unknownbuntu which is compatible
    with Ubuntu 15.10 you will still have to give an explicit platform
    string.
    """
    # FIXME: we should have a fallback for lsb_release, since platform.dist
    # is going away.

    try:
        with open("/etc/os-release") as osr:
            osReleaseLines = osr.readlines()
        hasOsRelease = True
    except (IOError, OSError):
        osReleaseLines = []
        hasOsRelease = False

    try:
        import platform
        platformTuple = platform.dist()
        platformSystem = platform.system()
        platformProcessor = platform.processor()
        if " " in platformProcessor:
            platformProcessor = platform.machine()
        return doDetectArch(hasOsRelease,
                            osReleaseLines,
                            platformTuple,
                            platformSystem,
                            platformProcessor)
    except:
        return None


def getVersion():
    try:
        import pkg_resources  # part of setuptools
        return pkg_resources.require("alibuild")[0].version
    except:
        git_dir = dirname(dirname(__file__))
        cmd = "GIT_DIR=\'%s/.git\' git describe --tags" % git_dir
        err, version = getstatusoutput(cmd)
        return version if not err else "Unknown version."


def filterByArchitecture(arch, requires):
    for r in requires:
        require, matcher = ":" in r and r.split(":", 1) or (r, ".*")
        if re.match(matcher, arch):
            yield require


def readDefaults(configDir, defaults, error):
    defaultsFilename = "%s/defaults-%s.sh" % (configDir, defaults)
    if not exists(defaultsFilename):
        viableDefaults = []
        for x in glob.glob("%s/defaults-*.sh" % configDir):
            viableDefaults.append(
                "- " + basename(x).replace("defaults-", "").replace(".sh", "")
            )
        viableDefaults = "\n".join(viableDefaults)

        msg = "Default `{0}' does not exist. Viable options:\n{1}".format(
            defaults or "<no defaults specified>",
            viableDefaults
        )
        error(msg)

    reader = getRecipeReader(defaultsFilename)
    err, defaultsMeta, defaultsBody = parseRecipe(reader)
    if err:
        error(err)
        exit(1)
    return (defaultsMeta, defaultsBody)


def getRecipeReader(url, dist=None):
    """Get the appropriate recipe reader depending on th filename."""
    m = re.search(r'^dist:(.*)@([^@]+)$', url)
    return GitReader(url, dist) if m and dist else FileReader(url)


class FileReader(object):
    """Read a recipe from a file"""
    def __init__(self, url):
        self.url = url

    def __call__(self):
        return open(self.url).read()


class GitReader(object):
    """Read a recipe from a git repository using git show."""
    def __init__(self, url, configDir):
        self.url, self.configDir = url, configDir

    def __call__(self):
        m = re.search(r'^dist:(.*)@([^@]+)$', self.url)
        fn, gh = m.groups()

        cmd = "GIT_DIR={0}/.git git show {1}:{2}.sh"
        cmd = cmd.format(self.configDir, gh, fn.lower())
        err, d = getstatusoutput(cmd)

        if err:
            msg = ("Cannot read recipe {0} from reference {1}.\n"
                   "Make sure you run first (this will not alter "
                   " your recipes):\n"
                   "   cd {2} && git remote update -p && git fetch --tags")
            raise RuntimeError(msg.format(fn, gh, self.configDir))

        return d


def yamlLoad(s):
    class YamlSafeOrderedLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader, node):
        loader.flatten_mapping(node)
        return OrderedDict(loader.construct_pairs(node))

    YamlSafeOrderedLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        construct_mapping
    )
    return yaml.load(s, YamlSafeOrderedLoader)


def yamlDump(s):
    class YamlOrderedDumper(yaml.SafeDumper):
        pass

    def represent_ordereddict(dumper, data):
        rep = []
        for k, v in data.items():
            k = dumper.represent_data(k)
            v = dumper.represent_data(v)
            rep.append((k, v))
        return yaml.nodes.MappingNode(u'tag:yaml.org,2002:map', rep)

    YamlOrderedDumper.add_representer(OrderedDict, represent_ordereddict)
    return yaml.dump(s, Dumper=YamlOrderedDumper)


def parseRecipe(reader):
    assert(reader.__call__)
    err, spec, recipe = (None, None, None)
    try:
        d = reader()
        header, recipe = d.split("---", 1)
        spec = yamlLoad(header)
        validateSpec(spec)
    except RuntimeError as e:
        err = str(e)
    except IOError as e:
        err = str(e)
    except SpecError as e:
        err = "Malformed header for %s\n%s" % (reader.url, str(e))
    except yaml.scanner.ScannerError as e:
        err = "Unable to parse %s\n%s" % (reader.url, str(e))
    except yaml.parser.ParserError as e:
        err = "Unable to parse %s\n%s" % (reader.url, str(e))
    except ValueError as e:
        err = "Unable to parse %s. Header missing." % reader.url
    return err, spec, recipe


# (Almost pure part of the defaults parsing)
# Override defaultsGetter for unit tests.
def parseDefaults(disable, defaultsGetter, log):
    """Defaults are actually special packages. They can override metadata
    of any other package and they can disable other packages. For
    example they could decide to switch from ROOT 5 to ROOT 6 and they
    could disable alien for O2. For this reason we need to parse their
    metadata early and extract the override and disable data.
    """
    defaultsMeta, defaultsBody = defaultsGetter()
    defaultsDisable = asList(defaultsMeta.get("disable", []))
    for x in defaultsDisable:
        log("Package %s has been disabled by current default." % x)

    disable.extend(defaultsDisable)

    if type(defaultsMeta.get("overrides", OrderedDict())) != OrderedDict:
        return ("overrides should be a dictionary", None, None)

    overrides, taps = OrderedDict(), {}
    commonEnv = {"env": defaultsMeta["env"]} if "env" in defaultsMeta else {}
    overrides["defaults-release"] = commonEnv
    for k, v in defaultsMeta.get("overrides", {}).items():
        f = k.split("@", 1)[0].lower()
        if "@" in k:
            taps[f] = "dist:"+k
        overrides[f] = dict(**(v or {}))
    return (None, overrides, taps)


def getPackageList(packages, specs, configDir, preferSystem, noSystem,
                   architecture, disable, defaults, dieOnError,
                   performPreferCheck, performRequirementCheck,
                   performValidateDefaults, overrides, taps, log):
    systemPackages = set()
    ownPackages = set()
    failedRequirements = set()
    testCache = {}
    requirementsCache = {}
    packages = packages[:]

    # Empty list = all OK
    # Non-empty list: list of valid defaults
    # None = no valid default
    validDefaults = []

    while packages:
        p = packages.pop(0)
        if p in specs:
            continue

        lowerPkg = p.lower()
        filename = taps.get(lowerPkg, "%s/%s.sh" % (configDir, lowerPkg))
        err, spec, recipe = parseRecipe(getRecipeReader(filename, configDir))
        dieOnError(err, err)
        dieOnError(
            spec["package"].lower() != lowerPkg,
            "%s.sh has different package field: %s" % (p, spec["package"])
        )
        dieOnError(
            spec["package"] != p,
            "%s should be spelt %s." % (p, spec["package"])
        )

        # If an override fully matches a package, we apply it. This means
        # you can have multiple overrides being applied for a given package.
        for override in overrides:
            if not re.match("^" + override.strip("^$") + "$", lowerPkg):
                continue

            msg = "Overrides for package {0}: {1}"
            log(msg.format(spec["package"], overrides[override]))
            spec.update(overrides.get(override, {}) or {})

        # If --always-prefer-system is passed or if prefer_system is set
        # to true inside the recipe, use the script specified in the
        # prefer_system_check stanza to see if we can use the system
        # version of the package.
        archOK = re.match(spec.get("prefer_system", "(?!.*)"), architecture)
        if not noSystem and (preferSystem or archOK):
            cmd = spec.get("prefer_system_check", "false").strip()
            if not spec["package"] in testCache:
                testCache[spec["package"]] = performPreferCheck(spec,
                                                                cmd.strip())

            err, output = testCache[spec["package"]]

            if not err:
                systemPackages.update([spec["package"]])
                disable.append(spec["package"])
            else:
                ownPackages.update([spec["package"]])

        dieOnError(
            "system_requirement" in spec and recipe.strip("\n\t "),
            "System requirements %s cannot have a recipe" % spec["package"]
        )

        if re.match(spec.get("system_requirement", "(?!.*)"), architecture):
            cmd = spec.get("system_requirement_check", "false")
            if not spec["package"] in requirementsCache:
                check = performRequirementCheck(spec, cmd.strip())
                requirementsCache[spec["package"]] = check

            err, output = requirementsCache[spec["package"]]
            if err:
                failedRequirements.update([spec["package"]])
                spec["version"] = "failed"
            else:
                disable.append(spec["package"])

        if spec["package"] in disable:
            continue

        # Check whether the package is compatible with the specified defaults
        if validDefaults is not None:
            (ok, msg, valid) = performValidateDefaults(spec)
            if valid:
                if not validDefaults:
                    validDefaults = valid[:]
                else:
                    validDefaults = [v for v in validDefaults if v in valid]

                if not validDefaults:
                    # no valid default works for all current packages
                    validDefaults = None

        # For the moment we treat build_requires just as requires.
        def fn(what):
            return filterByArchitecture(architecture, spec.get(what, []))

        spec["requires"] = [x for x in fn("requires") if x not in disable]
        spec["build_requires"] = [
            x for x in fn("build_requires") if x not in disable
        ]

        if spec["package"] != "defaults-release":
            spec["build_requires"].append("defaults-release")
        spec["runtime_requires"] = spec["requires"]
        spec["requires"] = spec["runtime_requires"] + spec["build_requires"]

        # Check that version is a string
        dieOnError(
            not isinstance(spec["version"], str),
            "In recipe \"%s\": version must be a string" % p
        )
        spec["tag"] = spec.get("tag", spec["version"])
        spec["version"] = spec["version"].replace("/", "_")
        spec["recipe"] = recipe.strip("\n")
        specs[spec["package"]] = spec
        packages += spec["requires"]

    return (systemPackages, ownPackages, failedRequirements, validDefaults)


def dockerStatusOutput(cmd, dockerImage=None, executor=getstatusoutput):
    if dockerImage:
        DOCKER_WRAPPER = ("docker run {0} "
                          "bash -c 'eval \"$(echo {1} | base64 --decode)\"'")

        try:
            encodedCommand = base64.b64encode(bytes(cmd,  encoding="ascii"))
            encodedCommand = encodedCommand.decode()
        except TypeError:
            encodedCommand = base64.b64encode(cmd)

        cmd = DOCKER_WRAPPER.format(dockerImage, encodedCommand)

    return executor(cmd)


class Hasher:
    def __init__(self):
        self.h = hashlib.sha1()

    def __call__(self, txt):
        if not type(txt) == bytes:
            txt = txt.encode('utf-8', 'ignore')
        self.h.update(txt)

    def hexdigest(self):
        return self.h.hexdigest()

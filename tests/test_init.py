from __future__ import print_function
# Assuming you are using the mock library to ... mock things
try:
    from unittest.mock import patch, call  # In Python 3, mock is built-in
    from io import StringIO
    PY3 = True
except ImportError:
    PY3 = False
    from mock import patch, call  # Python 2
    from StringIO import StringIO

try:
    from collections import OrderedDict
except ImportError:
    from ordereddict import OrderedDict

# import mock
import unittest
from argparse import Namespace
import os.path as path

from alibuild_helpers.init import doInit, parsePackagesDefinition


def can_do_git_clone(x):
    return 0


def valid_recipe(x):
    if "zlib" in x.url:
        return (
            0,
            {"package": "zlib",
             "source": "https://github.com/alisw/zlib",
             "version": "v1.0"},
            ""
        )
    elif "aliroot" in x.url:
        return (
            0,
            {"package": "AliRoot",
             "source": "https://github.com/alisw/AliRoot",
             "version": "master"},
            ""
        )

    # return (0, {}, "")


def dummy_exists(x):
    calls = {'/sw/MIRROR/aliroot': True}
    return calls.get(x, False)


CLONE_EVERYTHING = [
    call(u'git clone https://github.com/alisw/alidist -b master /alidist'),
    call(u'git clone https://github.com/alisw/AliRoot -b v5-08-00 --reference /sw/MIRROR/aliroot ./AliRoot && cd ./AliRoot && git remote set-url --push origin https://github.com/alisw/AliRoot')
]


class InitTestCase(unittest.TestCase):
    def test_packageDefinition(self):
        self.assertEqual(
            parsePackagesDefinition("AliRoot@v5-08-16,AliPhysics@v5-08-16-01"),
            [{'ver': 'v5-08-16', 'name': 'AliRoot'},
             {'ver': 'v5-08-16-01', 'name': 'AliPhysics'}]
        )
        self.assertEqual(
            parsePackagesDefinition("AliRoot,AliPhysics@v5-08-16-01"),
            [{'ver': '', 'name': 'AliRoot'},
             {'ver': 'v5-08-16-01', 'name': 'AliPhysics'}]
        )

    @patch("alibuild_helpers.init.info")
    # @patch("alibuild_helpers.init.path")
    @patch("alibuild_helpers.init.os")
    def test_doDryRunInit(self, mock_os, mock_info):
        fake_dist = {"repo": "alisw/alidist", "ver": "master"}
        args = Namespace(
            develPrefix=".",
            configDir="/alidist",
            pkgname="zlib,AliRoot@v5-08-00",
            referenceSources="/sw/MIRROR",
            dist=fake_dist,
            defaults="release",
            dryRun=True,
            fetchRepos=False
        )
        self.assertRaises(SystemExit, doInit, args)
        self.assertEqual(
            mock_info.mock_calls,
            [call((
                "This will initialise local checkouts for zlib,AliRoot\n"
                "--dry-run / -n specified. Doing nothing."))]
        )

    @patch("alibuild_helpers.init.banner")
    @patch("alibuild_helpers.init.info")
    # @patch("alibuild_helpers.init.path")
    # @patch("alibuild_helpers.init.os")
    @patch("os.mkdir")
    @patch("os.makedirs")
    @patch("os.path.join")
    @patch("os.path.exists")
    @patch("alibuild_helpers.init.execute")
    @patch("alibuild_helpers.utilities.parseRecipe")
    @patch("alibuild_helpers.init.updateReferenceRepoSpec")
    # @patch("alibuild_helpers.utilities.open")
    @patch("builtins.open" if PY3 else "__builtin__.open")
    @patch("alibuild_helpers.init.readDefaults")
    def test_doRealInit(self, mock_read_defaults, mock_open,
                        mock_update_reference, mock_parse_recipe,
                        mock_execute, mock_mkdir, mock_makedirs,
                        mock_join, mock_exists,
                        mock_info, mock_banner):
        fake_dist = {"repo": "alisw/alidist", "ver": "master"}

        def fake_news(x):
            return {
                "/alidist/defaults-release.sh": StringIO("package: defaults-release\nversion: v1\n---"),
                "/alidist/aliroot.sh": StringIO("package: AliRoot\nversion: master\nsource: https://github.com/alisw/AliRoot\n---")
            }[x]
        mock_open.side_effect = fake_news

        mock_execute.side_effect = can_do_git_clone
        mock_parse_recipe.side_effect = valid_recipe
        mock_exists.side_effect = dummy_exists
        mock_mkdir.return_value = None
        mock_makedirs.return_value = None
        mock_join.side_effect = path.join
        mock_read_defaults.return_value = (
            OrderedDict({
                "package": "defaults-release",
                "disable": []
            }),
            ""
        )
        args = Namespace(
            develPrefix=".",
            configDir="/alidist",
            pkgname="AliRoot@v5-08-00",
            referenceSources="/sw/MIRROR",
            dist=fake_dist,
            defaults="release",
            dryRun=False,
            fetchRepos=False
        )

        doInit(args)
        mock_execute.assert_called_with(
            ("git clone https://github.com/alisw/AliRoot "
             "-b v5-08-00 "
             "--reference /sw/MIRROR/aliroot "
             "./AliRoot && "
             "cd ./AliRoot && "
             "git remote set-url --push origin "
             "https://github.com/alisw/AliRoot")
        )
        self.assertEqual(mock_execute.mock_calls, CLONE_EVERYTHING)
        mock_exists.assert_has_calls([
            call('.'),
            call('/sw/MIRROR'),
            call('/alidist'),
            call('./AliRoot')
        ])

        # Force fetch repos
        mock_execute.reset_mock()
        mock_join.reset_mock()
        mock_exists.reset_mock()
        args.fetchRepos = True
        doInit(args)
        mock_execute.assert_called_with(
            ("git clone https://github.com/alisw/AliRoot "
             "-b v5-08-00 "
             "--reference /sw/MIRROR/aliroot "
             "./AliRoot && "
             "cd ./AliRoot && "
             "git remote set-url --push origin "
             "https://github.com/alisw/AliRoot")
        )
        self.assertEqual(mock_execute.mock_calls, CLONE_EVERYTHING)
        mock_exists.assert_has_calls([
            call('.'),
            call('/sw/MIRROR'),
            call('/alidist'),
            call('./AliRoot')
        ])


if __name__ == '__main__':
    unittest.main()

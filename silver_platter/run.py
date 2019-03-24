#!/usr/bin/python3
# Copyright (C) 2018 Jelmer Vernooij <jelmer@jelmer.uk>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA

"""Automatic proposal/push creation."""

from __future__ import absolute_import

import os
import subprocess

import silver_platter  # noqa: F401

from breezy import osutils
from breezy import (
    branch as _mod_branch,
    errors,
    )
from breezy.commit import PointlessCommit
from breezy.trace import note, show_error
from breezy.plugins.propose import (
    propose as _mod_propose,
    )
from .proposal import (
    BranchChanger,
    propose_or_push,
    )


class ScriptMadeNoChanges(errors.BzrError):

    _fmt = "Script made no changes."


def script_runner(local_tree, script):
    """Run a script in a tree and commit the result.

    This ignores newly added files.

    :param local_tree: Local tree to run script in
    :param script: Script to run
    :return: Description as reported by script
    """
    p = subprocess.Popen(script, cwd=local_tree.basedir,
                         stdout=subprocess.PIPE, shell=True)
    (description, err) = p.communicate("")
    if p.returncode != 0:
        raise errors.BzrCommandError(
            "Script %s failed with error code %d" % (
                script, p.returncode))
    description = description.decode()
    try:
        local_tree.commit(description, allow_pointless=False)
    except PointlessCommit:
        raise ScriptMadeNoChanges()
    return description


class ScriptBranchChanger(BranchChanger):

    def __init__(self, script):
        self._script = script
        self._description = None
        self._create_proposal = None

    def make_changes(self, local_tree):
        try:
            self._description = script_runner(local_tree, self._script)
        except ScriptMadeNoChanges:
            self._create_proposal = False
        else:
            self._create_proposal = True

    def get_proposal_description(self, existing_proposal):
        if self._description is not None:
            return self._description
        if existing_proposal is not None:
            return existing_proposal.get_description()
        raise ValueError("No description available")

    def should_create_proposal(self):
        return self._create_proposal


def setup_parser(parser):
    parser.add_argument('url', help='URL of branch to work on.', type=str)
    parser.add_argument('script', help='Path to script to run.', type=str)
    parser.add_argument('--refresh', action="store_true",
                        help='Refresh changes if branch already exists')
    parser.add_argument('--label', type=str,
                        help='Label to attach', action="append", default=[])
    parser.add_argument('--name', type=str,
                        help='Proposed branch name', default=None)
    parser.add_argument(
        '--mode',
        help='Mode for pushing', choices=['push', 'attempt-push', 'propose'],
        default="propose", type=str)
    parser.add_argument(
        "--dry-run",
        help="Create branches but don't push or propose anything.",
        action="store_true", default=False)


def main(args):
    main_branch = _mod_branch.Branch.open(args.url)
    if args.name is None:
        name = os.path.splitext(osutils.basename(args.script.split(' ')[0]))[0]
    else:
        name = args.name
    try:
        result = propose_or_push(
                main_branch, name, ScriptBranchChanger(args.script),
                refresh=args.refresh, labels=args.label,
                mode=args.mode, dry_run=args.dry_run)
    except _mod_propose.UnsupportedHoster as e:
        show_error('No known supported hoster for %s. Run \'svp login\'?',
                   e.branch.user_url)
        return 1
    except _mod_propose.HosterLoginRequired as e:
        show_error(
            'Credentials for hosting site at %r missing. Run \'svp login\'?',
            e.hoster.base_url)
        return 1
    except ScriptMadeNoChanges:
        show_error('Script did not make any changes.')
        return 1
    if result.merge_proposal:
        note('Merge proposal created: %s', result.merge_proposal.url)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    setup_parser(parser)
    args = parser.parse_args()
    main(args)
